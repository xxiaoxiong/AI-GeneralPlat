"""
工具安全策略：权限分级、SSRF 防护、路径守卫、HITL 工具集。

原则：模型端隐藏 ≠ 服务端授权，执行前必须再次检查。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from app.core.config import settings


class ToolRisk(str, Enum):
    READ = "read"            # 只读
    WRITE = "write"          # 写入
    NETWORK = "network"      # 网络
    EXECUTE = "execute"      # 代码执行
    DANGEROUS = "dangerous"  # 危险操作


# 副作用等级（与 classification.py 常量取值保持一致）
CLASS_READ_ONLY = "read_only"
CLASS_NON_IDEMPOTENT = "non_idempotent"

# 重试策略（与 classification.py 常量取值保持一致）
RETRY_SAFE = "safe_retry"
RETRY_NONE = "none"


@dataclass(frozen=True)
class ToolRiskProfile:
    """工具风险登记（§15.2 A）：每个工具显式声明风险四元组。

    - ``risk``：风险等级（READ/WRITE/NETWORK/EXECUTE/DANGEROUS）；
    - ``side_effect_class``：副作用等级（read_only / external_idempotent / non_idempotent）；
    - ``requires_approval``：是否需要 HITL 人工审批；
    - ``retry_policy``：结果不确定时的重试策略（safe_retry / idempotent_key / none）；
    - network_access / exec_access / file_access：能力位（未登记工具不自动获得任何能力）。
    """

    name: str
    risk: ToolRisk
    side_effect_class: str = CLASS_NON_IDEMPOTENT
    requires_approval: bool = False
    retry_policy: str = RETRY_NONE
    network_access: bool = False
    exec_access: bool = False
    file_access: bool = False


# 工具风险显式注册表（§15.2 A）：未登记工具默认拒绝（不得默认 READ / 不得自动获得能力）。
# 覆盖平台内置工具（app/services/tools BUILTIN_TOOLS，含 http_request 别名）与
# DeepAgents 虚拟文件工具；自定义工具必须经 register_tool_risk 显式登记后才可执行。
_TOOL_RISK_REGISTRY: Dict[str, ToolRiskProfile] = {}


def register_tool_risk(profile: ToolRiskProfile) -> None:
    """登记工具风险分类（自定义工具 / 部署扩展必须显式登记后才可执行）。"""
    _TOOL_RISK_REGISTRY[profile.name] = profile


def unregister_tool_risk(name: str) -> None:
    """移除登记（测试 / 配置重载用）。"""
    _TOOL_RISK_REGISTRY.pop(name, None)


def get_tool_risk(name: str) -> Optional[ToolRiskProfile]:
    """查询风险登记；未登记返回 None（调用方必须按拒绝处理）。"""
    return _TOOL_RISK_REGISTRY.get(name)


def _bootstrap_builtin_profiles() -> None:
    """内置工具风险登记：side-effect class / risk / approval / retry 全部显式声明。"""
    read_only = dict(side_effect_class=CLASS_READ_ONLY, retry_policy=RETRY_SAFE)
    non_idem = dict(side_effect_class=CLASS_NON_IDEMPOTENT, retry_policy=RETRY_NONE)
    entries = [
        # 只读计算 / 数据 / 检索
        ToolRiskProfile("datetime_now", ToolRisk.READ, **read_only),
        ToolRiskProfile("calculator", ToolRisk.READ, **read_only),
        ToolRiskProfile("db_schema", ToolRisk.READ, **read_only),
        ToolRiskProfile("db_query", ToolRisk.READ, **read_only),
        ToolRiskProfile("db_count", ToolRisk.READ, **read_only),
        ToolRiskProfile("db_aggregate", ToolRisk.READ, **read_only),
        ToolRiskProfile("knowledge_search", ToolRisk.READ, **read_only),
        # 网络只读（GET / 搜索 / 抓取 / 天气）
        ToolRiskProfile("http_get", ToolRisk.NETWORK, network_access=True, **read_only),
        ToolRiskProfile("http_request", ToolRisk.NETWORK, network_access=True, **read_only),
        ToolRiskProfile("fetch_webpage", ToolRisk.NETWORK, network_access=True, **read_only),
        ToolRiskProfile("web_search", ToolRisk.NETWORK, network_access=True, **read_only),
        ToolRiskProfile("weather", ToolRisk.NETWORK, network_access=True, **read_only),
        # 网络副作用（POST / 邮件 / Webhook）：不可幂等、需审批、禁止自动重试
        ToolRiskProfile("http_post", ToolRisk.NETWORK, requires_approval=True,
                        network_access=True, **non_idem),
        ToolRiskProfile("send_email_tool", ToolRisk.NETWORK, requires_approval=True,
                        network_access=True, **non_idem),
        ToolRiskProfile("webhook_notify", ToolRisk.NETWORK, requires_approval=True,
                        network_access=True, **non_idem),
        # 代码执行：需审批；restricted 不是安全沙箱（见 check_exec_allowed）
        ToolRiskProfile("python_exec", ToolRisk.EXECUTE, requires_approval=True,
                        exec_access=True, **non_idem),
        ToolRiskProfile("execute", ToolRisk.EXECUTE, requires_approval=True,
                        exec_access=True, **non_idem),
        # DeepAgents 虚拟文件工具
        ToolRiskProfile("read_file", ToolRisk.READ, file_access=True, **read_only),
        ToolRiskProfile("ls", ToolRisk.READ, file_access=True, **read_only),
        ToolRiskProfile("glob", ToolRisk.READ, file_access=True, **read_only),
        ToolRiskProfile("grep", ToolRisk.READ, file_access=True, **read_only),
        ToolRiskProfile("write_file", ToolRisk.WRITE, file_access=True, **non_idem),
        ToolRiskProfile("edit_file", ToolRisk.WRITE, file_access=True, **non_idem),
        ToolRiskProfile("delete_file", ToolRisk.DANGEROUS, requires_approval=True,
                        file_access=True, **non_idem),
    ]
    for p in entries:
        register_tool_risk(p)


_bootstrap_builtin_profiles()

# 需要 HITL 审批的工具（默认集合，可由配置收窄/放开）
DEFAULT_HITL_TOOLS: Set[str] = {
    "python_exec",
    "http_post",
    "webhook_notify",
    "send_email_tool",
    "delete_file",
}

# 高危工具：内网/生产默认禁用
DANGEROUS_TOOLS: Set[str] = {"python_exec", "execute", "delete_file"}

# 代码执行类工具（exec_access）：restricted 进程内限制 ≠ 安全沙箱
EXEC_ACCESS_TOOLS: Set[str] = {"python_exec", "execute"}

# SQL 只读允许的语句首词（db_query 仅允许只读）
SQL_READ_HEADS = frozenset({"select", "with", "show", "describe", "desc", "explain"})

# 语句体任何位置（词边界）出现即拒绝的写入/DDL/DCL 词（CTE 内写入同样拦截）
SQL_FORBIDDEN_WORDS = frozenset({
    "insert", "update", "delete", "drop", "alter", "truncate",
    "create", "grant", "revoke", "merge",
})

# SQL 写入/DDL/DCL 关键词的词边界匹配（避免 updated_at / updates 等误伤）
_SQL_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(SQL_FORBIDDEN_WORDS)) + r")\b", re.IGNORECASE
)


def _strip_sql_noise(sql: str) -> str:
    """剥离 SQL 注释与字符串字面量（替换为空格），供只读判定使用。

    支持 -- 行注释、# 行注释、/* */ 块注释、'...' 与 "..." 字符串
    （含 '' / "" 转义与反斜杠转义）。判定语义保守：剥离后再做关键词扫描。
    """
    out: List[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        if ch == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        # 块注释
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        # 字符串字面量（' 或 "，支持成对转义与反斜杠转义）
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:  # '' / "" 转义
                        j += 2
                        continue
                    break
                j += 1
            i = min(j + 1, n)
            out.append("''")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class ToolPolicyError(Exception):
    """策略拒绝（执行前拦截）"""


class BudgetExceededError(ToolPolicyError):
    """执行预算超限（FR-10.7）：必须映射为 limit_exceeded，不得误报 failed。"""


@dataclass
class ToolPolicy:
    """一次运行的工具授权与安全边界。"""

    allowed_tools: Set[str]
    hitl_tools: Set[str] = field(default_factory=lambda: set(DEFAULT_HITL_TOOLS))
    enable_hitl: bool = True
    sandbox_mode: str = "restricted"     # restricted / disabled / external
    allow_network: bool = True
    network_allowlist: List[str] = field(default_factory=list)   # 域名/前缀，空 = 全部允许但做 SSRF 内网拦截
    blocked_network_cidrs: List[str] = field(default_factory=list)
    verify_tls: bool = True
    workspace_root: Optional[str] = None  # 文件工具允许的根目录
    # 显式开发开关（§15.2 F）：无外部 Sandbox 时允许进程内受限 python_exec；
    # 仅限开发环境（from_settings 在生产下强制关闭），默认 False。
    allow_unsafe_python: bool = False

    @classmethod
    def from_settings(cls, allowed_tools: Set[str], enable_hitl: bool = True) -> "ToolPolicy":
        allowlist = []
        raw = getattr(settings, "AGENT_NETWORK_ALLOWLIST", "")
        if raw:
            allowlist = [x.strip() for x in raw.split(",") if x.strip()]
        blocked = []
        raw_blocked = getattr(settings, "AGENT_NETWORK_BLOCKED_CIDRS", "")
        if raw_blocked:
            blocked = [x.strip() for x in raw_blocked.split(",") if x.strip()]
        return cls(
            allowed_tools=set(allowed_tools),
            enable_hitl=enable_hitl,
            sandbox_mode=getattr(settings, "AGENT_SANDBOX_MODE", "restricted"),
            allow_network=getattr(settings, "AGENT_ALLOW_NETWORK", True),
            network_allowlist=allowlist,
            blocked_network_cidrs=blocked,
            verify_tls=getattr(settings, "AGENT_TLS_VERIFY", True),
            # 开发逃生开关仅在非生产生效（§15.2 F：生产无外部 Sandbox 默认禁用）
            allow_unsafe_python=(
                bool(getattr(settings, "AGENT_ALLOW_UNSAFE_PYTHON", False))
                and not settings.is_production
            ),
        )

    # ── 授权检查 ──────────────────────────────────────────────────────────

    def check_tool_allowed(self, tool_name: str) -> None:
        # §15.2 A：未登记风险分类的工具默认拒绝（fail-closed），
        # 不得默认 READ、不得自动获得网络/执行/文件能力
        if get_tool_risk(tool_name) is None:
            raise ToolPolicyError(
                f"工具 '{tool_name}' 未登记风险分类（side-effect/risk/approval/retry），默认拒绝执行；"
                "请先通过 register_tool_risk 显式登记"
            )
        if tool_name not in self.allowed_tools:
            raise ToolPolicyError(f"工具 '{tool_name}' 未授权给当前 Agent")

    def requires_approval(self, tool_name: str) -> bool:
        if not self.enable_hitl:
            return False
        profile = get_tool_risk(tool_name)
        if profile is None:
            return True  # 未登记工具默认要求人工审批（执行已被 check_tool_allowed 拒绝）
        return tool_name in self.hitl_tools or profile.requires_approval

    def check_exec_allowed(self, tool_name: str) -> None:
        """执行类工具的沙箱门禁（§15.2 F）。

        - 未登记工具默认拒绝；
        - disabled：全部高危工具禁用；
        - restricted：进程内 AST/线程限制**不是安全沙箱**，代码执行类工具
          （python_exec / execute）默认拒绝；仅开发环境显式
          AGENT_ALLOW_UNSAFE_PYTHON 开关可放行；
        - external：接入外部隔离沙箱后放行。
        """
        profile = get_tool_risk(tool_name)
        if profile is None:
            raise ToolPolicyError(
                f"工具 '{tool_name}' 未登记风险分类，默认拒绝执行"
            )
        if profile.risk not in (ToolRisk.EXECUTE, ToolRisk.DANGEROUS):
            return
        if self.sandbox_mode == "disabled":
            raise ToolPolicyError(
                f"工具 '{tool_name}' 属于高危执行类工具，当前沙箱模式为 disabled（默认禁用）。"
                "如需启用请配置 AGENT_SANDBOX_MODE 并接入隔离沙箱。"
            )
        if profile.exec_access and self.sandbox_mode != "external" and not self.allow_unsafe_python:
            raise ToolPolicyError(
                f"工具 '{tool_name}' 为代码执行类工具：restricted（进程内 AST/线程限制）"
                "不是安全沙箱，生产模式默认禁用。请接入外部 Sandbox"
                "（AGENT_SANDBOX_MODE=external），或仅在开发环境显式设置 "
                "AGENT_ALLOW_UNSAFE_PYTHON=true。"
            )

    # ── SSRF / 网络防护 ──────────────────────────────────────────────────

    def check_url(self, url: str) -> None:
        if not self.allow_network:
            raise ToolPolicyError("当前运行禁止网络访问")
        parsed = urlparse(url if "://" in url else f"http://{url}")
        if parsed.scheme not in ("http", "https"):
            raise ToolPolicyError(f"不允许的 URL scheme: {parsed.scheme}")
        host = parsed.hostname or ""
        if not host:
            raise ToolPolicyError("URL 缺少主机名")

        if self.network_allowlist:
            # 只允许 host 完全等于或为受信任域名的子域；禁止前缀字符串匹配
            # （trusted.example.evil 不得因 startswith("trusted.example") 误过）
            if not any(host == a or host.endswith("." + a) for a in self.network_allowlist):
                raise ToolPolicyError(f"目标主机 '{host}' 不在受信任 allowlist 内")
            # 显式 allowlist 是刻意信任决策（内网部署常见），跳过内网/回环拦截
            return

        # 回环 / 云元数据 / 内网地址拦截
        self._check_host_not_internal(host, parsed.port)

    def _check_host_not_internal(self, host: str, port: Optional[int]) -> None:
        metadata_hosts = {"metadata.google.internal", "metadata.goog", "169.254.169.254"}
        if host.lower() in metadata_hosts:
            raise ToolPolicyError("禁止访问云元数据地址")
        try:
            infos = socket.getaddrinfo(host, port or 80, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise ToolPolicyError(f"DNS 解析失败: {host} ({e})")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_private:
                raise ToolPolicyError(f"禁止访问内网/回环地址: {host} -> {ip}")
            for cidr in self.blocked_network_cidrs:
                if ip in ipaddress.ip_network(cidr, strict=False):
                    raise ToolPolicyError(f"目标 IP {ip} 命中封禁网段 {cidr}")

    # ── 路径守卫 ──────────────────────────────────────────────────────────

    def check_path(self, path: str) -> None:
        """文件工具路径防穿越：仅允许 workspace_root 之下（含符号链接逃逸防护）。

        §15.2 C：root 与目标都必须经 os.path.realpath 解析符号链接后再比较，
        防止工作区内符号链接指向外部文件绕过路径守卫。
        """
        import os

        if not self.workspace_root:
            raise ToolPolicyError("文件工具未配置工作区根目录，拒绝访问")
        if os.path.isabs(path) or path.startswith("/") or path.startswith("\\") or (len(path) > 1 and path[1] == ":"):
            raise ToolPolicyError(f"不允许绝对路径: {path}")
        # realpath 解析符号链接（root 与 target 同规则），防止链接逃逸
        root = os.path.realpath(os.path.abspath(self.workspace_root))
        target = os.path.realpath(os.path.abspath(os.path.join(self.workspace_root, path)))
        if not (target == root or target.startswith(root + os.sep)):
            raise ToolPolicyError(f"路径越界（含符号链接逃逸）: {path}")

    # ── SQL 只读守卫 ─────────────────────────────────────────────────────

    def check_sql_readonly(self, sql: str) -> None:
        """db_query 只读 SQL 守卫（§15.2 D，fail-closed）。

        - 剥离注释与字符串字面量后判断，防止注释/字符串拼接绕过；
        - 拒绝多语句（分号只允许出现在语句末尾）；
        - 首词必须是 SELECT/WITH/SHOW/DESCRIBE/EXPLAIN；
        - 语句体任何位置（词边界）出现写入/DDL/DCL 关键词即拒绝
          （堵住 WITH ... AS (INSERT ...) 等 CTE 写入绕过）。
        """
        cleaned = _strip_sql_noise(sql).strip()
        if not cleaned:
            raise ToolPolicyError("db_query 仅允许只读 SQL，拒绝空语句")
        # 多语句拒绝：只允许末尾一个分号，其余任何分号即多语句
        body = cleaned[:-1].rstrip() if cleaned.endswith(";") else cleaned
        if ";" in body:
            raise ToolPolicyError("db_query 禁止多语句 SQL")
        head = body.split(None, 1)[0].lower().lstrip("(") if body else ""
        if head not in SQL_READ_HEADS:
            raise ToolPolicyError(f"db_query 仅允许只读 SQL，拒绝: {head or '(空)'}")
        for m in _SQL_WORD_RE.finditer(body):
            if m.group(0).lower() in SQL_FORBIDDEN_WORDS:
                raise ToolPolicyError(
                    f"db_query 仅允许只读 SQL，语句中出现写入类关键词: {m.group(0).lower()}"
                )

    def to_audit_fields(self) -> Dict[str, Any]:
        return {
            "allowed_tools": sorted(self.allowed_tools),
            "hitl_tools": sorted(self.hitl_tools),
            "sandbox_mode": self.sandbox_mode,
            "allow_network": self.allow_network,
            "verify_tls": self.verify_tls,
        }

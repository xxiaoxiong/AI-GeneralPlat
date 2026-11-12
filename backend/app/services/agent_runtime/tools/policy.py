"""
工具安全策略：权限分级、SSRF 防护、路径守卫、HITL 工具集。

原则：模型端隐藏 ≠ 服务端授权，执行前必须再次检查。
"""
from __future__ import annotations

import ipaddress
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


# 工具风险分级（未列出的默认 READ）
TOOL_RISK: Dict[str, ToolRisk] = {
    "http_get": ToolRisk.NETWORK,
    "http_post": ToolRisk.NETWORK,
    "http_request": ToolRisk.NETWORK,
    "send_email_tool": ToolRisk.NETWORK,
    "webhook_notify": ToolRisk.NETWORK,
    "fetch_webpage": ToolRisk.NETWORK,
    "web_search": ToolRisk.NETWORK,
    "python_exec": ToolRisk.EXECUTE,
    "execute": ToolRisk.EXECUTE,
    "write_file": ToolRisk.WRITE,
    "edit_file": ToolRisk.WRITE,
    "delete_file": ToolRisk.DANGEROUS,
}

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

# DB 写入类语句前缀（db_query 只允许只读）
DB_WRITE_PREFIXES = ("insert", "update", "delete", "drop", "alter", "truncate", "create", "grant", "revoke")


class ToolPolicyError(Exception):
    """策略拒绝（执行前拦截）"""


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
        )

    # ── 授权检查 ──────────────────────────────────────────────────────────

    def check_tool_allowed(self, tool_name: str) -> None:
        if tool_name not in self.allowed_tools:
            raise ToolPolicyError(f"工具 '{tool_name}' 未授权给当前 Agent")

    def requires_approval(self, tool_name: str) -> bool:
        return self.enable_hitl and tool_name in self.hitl_tools

    def check_exec_allowed(self, tool_name: str) -> None:
        """执行类工具的沙箱门禁。"""
        if tool_name in DANGEROUS_TOOLS and self.sandbox_mode == "disabled":
            raise ToolPolicyError(
                f"工具 '{tool_name}' 属于高危执行类工具，当前沙箱模式为 disabled（默认禁用）。"
                "如需启用请配置 AGENT_SANDBOX_MODE 并接入隔离沙箱。"
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
            if not any(host == a or host.endswith("." + a) or host.startswith(a) for a in self.network_allowlist):
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
        """文件工具路径防穿越：仅允许 workspace_root 之下。"""
        import os

        if not self.workspace_root:
            raise ToolPolicyError("文件工具未配置工作区根目录，拒绝访问")
        if os.path.isabs(path) or path.startswith("/") or path.startswith("\\") or (len(path) > 1 and path[1] == ":"):
            raise ToolPolicyError(f"不允许绝对路径: {path}")
        target = os.path.abspath(os.path.join(self.workspace_root, path))
        root = os.path.abspath(self.workspace_root)
        if not (target == root or target.startswith(root + os.sep)):
            raise ToolPolicyError(f"路径越界: {path}")

    # ── SQL 只读守卫 ─────────────────────────────────────────────────────

    def check_sql_readonly(self, sql: str) -> None:
        head = sql.strip().split(None, 1)[0].lower() if sql.strip() else ""
        if head in DB_WRITE_PREFIXES:
            raise ToolPolicyError(f"db_query 仅允许只读 SQL，拒绝: {head}")

    def to_audit_fields(self) -> Dict[str, Any]:
        return {
            "allowed_tools": sorted(self.allowed_tools),
            "hitl_tools": sorted(self.hitl_tools),
            "sandbox_mode": self.sandbox_mode,
            "allow_network": self.allow_network,
            "verify_tls": self.verify_tls,
        }

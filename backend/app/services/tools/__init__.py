"""
tools 包 - 只保留 LLM 自身无法完成的工具（实时数据 / 精确计算 / 真实 I/O）

  datetime_tools.py  📅 时间日期（datetime_now）
  math_tools.py      🔢 计算数学（calculator）
  network_tools.py   🌐 网络请求（http_get, http_post）
  database_tools.py  🗄️ 数据库  （db_query, db_count, db_aggregate）
  knowledge_tools.py 🔍 知识检索（knowledge_search）
  notify_tools.py    📧 通知推送（send_email_tool, webhook_notify）
  web_tools.py       🔎 网络增强（web_search, fetch_webpage, weather）
  code_tools.py      🐍 代码执行（python_exec）
"""
import json
from typing import Dict, Any, List

from .result import ToolResult, normalize

from . import (
    datetime_tools,
    math_tools,
    network_tools,
    database_tools,
    text_tools,
    knowledge_tools,
    analysis_tools,
    system_tools,
    notify_tools,
    web_tools,
    code_tools,
)

# ── 汇总所有工具注册表 ────────────────────────────────────────────────────────

BUILTIN_TOOLS: List[Dict[str, Any]] = (
    datetime_tools.TOOLS
    + math_tools.TOOLS
    + network_tools.TOOLS
    + database_tools.TOOLS
    + text_tools.TOOLS
    + knowledge_tools.TOOLS
    + analysis_tools.TOOLS
    + system_tools.TOOLS
    + notify_tools.TOOLS
    + web_tools.TOOLS
    + code_tools.TOOLS
)

# ── 工具路由表 ────────────────────────────────────────────────────────────────
# 按调用签名分类（handler 返回 str | ToolResult，execute 边界统一归一化）：
#   SYNC    : handler(params) -> str | ToolResult
#   ASYNC   : async handler(params) -> str | ToolResult
#   DB      : async handler(db, params) -> str | ToolResult
#   AGENT   : async handler(db, agent_config, params) -> str | ToolResult

_SYNC_HANDLERS: Dict[str, Any] = {
    **datetime_tools.HANDLERS,
    **math_tools.HANDLERS,
    **code_tools.HANDLERS,
}

_ASYNC_HANDLERS: Dict[str, Any] = {
    **network_tools.HANDLERS,
    **notify_tools.HANDLERS,
    **web_tools.HANDLERS,
}

_DB_HANDLERS: Dict[str, Any] = {
    **database_tools.HANDLERS,
}

_AGENT_HANDLERS: Dict[str, Any] = {
    **knowledge_tools.HANDLERS,
}


class ToolExecutor:
    """统一工具执行器，自动路由到对应分类模块"""

    def __init__(self, db, agent_config: dict):
        self.db           = db
        self.agent_config = agent_config

    async def execute(
        self, tool_name: str, tool_input: dict, *, raise_on_error: bool = False
    ) -> ToolResult:
        """执行工具并做统一结果归一化（P0-05 §7.2 A / §7.3）。

        - handler 返回 str 时归一化为 succeeded；返回 ToolResult 时原样透传；
        - 未知工具 -> failed；
        - ``raise_on_error=True`` 时异常向上抛（运行时工具账本据异常类型
          区分 failed / unknown）；否则归一化为 ToolResult.failed。
        """
        try:
            if tool_name in _SYNC_HANDLERS:
                return normalize(_SYNC_HANDLERS[tool_name](tool_input))

            if tool_name in _ASYNC_HANDLERS:
                return normalize(await _ASYNC_HANDLERS[tool_name](tool_input))

            if tool_name in _DB_HANDLERS:
                return normalize(await _DB_HANDLERS[tool_name](self.db, tool_input))

            if tool_name in _AGENT_HANDLERS:
                return normalize(
                    await _AGENT_HANDLERS[tool_name](self.db, self.agent_config, tool_input)
                )

            # 自定义 HTTP 工具
            custom = next(
                (t for t in self.agent_config.get("custom_tools", []) if t["name"] == tool_name),
                None,
            )
            if custom:
                return await self._custom_http_tool(custom, tool_input)

            return ToolResult.failed(f"错误：未知工具 '{tool_name}'")

        except Exception as e:
            if raise_on_error:
                # 运行时工具账本需要真实异常区分 failed / unknown，向上抛出
                raise
            return ToolResult.failed(f"工具执行错误: {e}")

    async def _custom_http_tool(self, tool_config: dict, params: dict) -> ToolResult:
        """自定义 HTTP 工具：非 2xx 一律 failed（§7.2 C，不得把错误码当成功）。"""
        import httpx
        url    = tool_config.get("url", "")
        method = tool_config.get("method", "GET").upper()
        for k, v in params.items():
            url = url.replace(f"{{{k}}}", str(v))
        from app.core.config import settings as _settings
        async with httpx.AsyncClient(
            timeout=15, verify=(_settings.AGENT_CA_BUNDLE or _settings.AGENT_TLS_VERIFY),
            proxy=(_settings.AGENT_EGRESS_PROXY or None),
        ) as client:
            resp = await client.get(url) if method == "GET" else await client.post(url, json=params)
        try:
            body = json.dumps(resp.json(), ensure_ascii=False)[:2000]
        except Exception:
            body = resp.text[:2000]
        if not (200 <= resp.status_code < 300):
            return ToolResult.failed(
                f"HTTP {resp.status_code} {method} {url}\n{body}",
                retryable=resp.status_code >= 500,
            )
        return ToolResult.succeeded(body)


def get_tool_by_name(name: str) -> Dict | None:
    return next((t for t in BUILTIN_TOOLS if t["name"] == name), None)

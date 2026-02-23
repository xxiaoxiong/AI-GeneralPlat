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
# 按调用签名分类：
#   SYNC    : handler(params) -> str
#   ASYNC   : async handler(params) -> str
#   DB      : async handler(db, params) -> str
#   AGENT   : async handler(db, agent_config, params) -> str

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

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name in _SYNC_HANDLERS:
                return _SYNC_HANDLERS[tool_name](tool_input)

            if tool_name in _ASYNC_HANDLERS:
                return await _ASYNC_HANDLERS[tool_name](tool_input)

            if tool_name in _DB_HANDLERS:
                return await _DB_HANDLERS[tool_name](self.db, tool_input)

            if tool_name in _AGENT_HANDLERS:
                return await _AGENT_HANDLERS[tool_name](self.db, self.agent_config, tool_input)

            # 自定义 HTTP 工具
            custom = next(
                (t for t in self.agent_config.get("custom_tools", []) if t["name"] == tool_name),
                None,
            )
            if custom:
                return await self._custom_http_tool(custom, tool_input)

            return f"错误：未知工具 '{tool_name}'"

        except Exception as e:
            return f"工具执行错误: {e}"

    async def _custom_http_tool(self, tool_config: dict, params: dict) -> str:
        import httpx
        url    = tool_config.get("url", "")
        method = tool_config.get("method", "GET").upper()
        for k, v in params.items():
            url = url.replace(f"{{{k}}}", str(v))
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(url) if method == "GET" else await client.post(url, json=params)
        try:
            return json.dumps(resp.json(), ensure_ascii=False)[:2000]
        except Exception:
            return resp.text[:2000]


def get_tool_by_name(name: str) -> Dict | None:
    return next((t for t in BUILTIN_TOOLS if t["name"] == name), None)

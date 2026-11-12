"""
业务工具桥接：BUILTIN_TOOLS 注册表 -> LangChain StructuredTool。

- 由 tools 注册表的 parameters/required 动态生成 pydantic 参数模型
- 执行前过 ToolPolicy 授权/SSRF/路径/沙箱检查
- 执行经原 ToolExecutor 路由（SYNC/ASYNC/DB/AGENT + 自定义 HTTP）
- 工具生命周期事件写入 run 级 EventBus
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Type

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from app.services.tools import BUILTIN_TOOLS, get_tool_by_name
from app.services.tools import ToolExecutor

from ..context import RuntimeContext
from ..events import (
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_STARTED,
    EventBus,
    RuntimeEvent,
    redact_sensitive,
)
from ..telemetry import telemetry
from .policy import ToolPolicy, ToolPolicyError

_TYPE_MAP: Dict[str, Type] = {
    "string": str,
    "str": str,
    "integer": int,
    "int": int,
    "number": float,
    "float": float,
    "boolean": bool,
    "bool": bool,
    "array": list,
    "object": dict,
}


def build_args_model(name: str, spec: Dict[str, Any]) -> Type[BaseModel]:
    """由注册表 parameters/required 生成 pydantic v2 参数模型。"""
    params = spec.get("parameters") or {}
    required = set(spec.get("required") or [])
    fields: Dict[str, Any] = {}
    for pname, pspec in params.items():
        ptype = _TYPE_MAP.get((pspec or {}).get("type", "string"), str)
        desc = (pspec or {}).get("description", "")
        if pname in required:
            fields[pname] = (ptype, Field(description=desc))
        else:
            fields[pname] = (Optional[ptype], Field(default=None, description=desc))
    if not fields:
        fields["_unused"] = (Optional[str], Field(default=None, description="无参数"))
    model = create_model(f"{_camel(name)}Args", **fields)
    return model


def _camel(s: str) -> str:
    return "".join(p.title() for p in s.replace("-", "_").split("_"))


class ToolAdapterFactory:
    """按运行上下文构造 LangChain 工具集。"""

    def __init__(self, ctx: RuntimeContext, policy: ToolPolicy, bus: EventBus):
        self.ctx = ctx
        self.policy = policy
        self.bus = bus

    def build(self, tool_specs: List[Dict[str, Any]]) -> List[StructuredTool]:
        tools: List[StructuredTool] = []
        for spec in tool_specs:
            name = spec["name"]
            if name not in self.policy.allowed_tools:
                continue
            tools.append(self._make_tool(spec))
        return tools

    def _make_tool(self, spec: Dict[str, Any]) -> StructuredTool:
        name = spec["name"]
        args_model = build_args_model(name, spec)
        description = spec.get("description") or spec.get("display_name") or name

        async def _coroutine(**kwargs: Any) -> str:
            return await self._execute(name, kwargs)

        return StructuredTool(
            name=name,
            description=description,
            args_schema=args_model,
            coroutine=_coroutine,
        )

    async def _execute(self, name: str, args: Dict[str, Any]) -> str:
        # 1) 授权 + 策略检查
        try:
            self.policy.check_tool_allowed(name)
            self.policy.check_exec_allowed(name)
            if name in ("http_get", "http_post", "http_request", "fetch_webpage"):
                url = args.get("url") or ""
                self.policy.check_url(str(url))
            if name in ("write_file", "edit_file", "read_file", "delete_file"):
                self.policy.check_path(str(args.get("path") or args.get("file_path") or ""))
            if name == "db_query" and args.get("sql"):
                self.policy.check_sql_readonly(str(args["sql"]))
        except ToolPolicyError as e:
            await self.bus.publish(RuntimeEvent(
                type=TOOL_FAILED,
                run_id=self.ctx.run_id,
                data={"tool": name, "error": f"策略拒绝: {e}"},
            ))
            telemetry.record_tool(
                self.ctx.to_log_fields(), tool=name, status="rejected", error=str(e),
            )
            raise

        # 2) 执行
        started = time.monotonic()
        await self.bus.publish(RuntimeEvent(
            type=TOOL_STARTED,
            run_id=self.ctx.run_id,
            data={"tool": name, "input": redact_sensitive(args)},
        ))
        self.ctx.tool_calls += 1
        reason = self.ctx.budget.exhausted_reason(self.ctx.steps, self.ctx.tool_calls, self.ctx.model_calls)
        if reason:
            await self.bus.publish(RuntimeEvent(
                type=TOOL_FAILED,
                run_id=self.ctx.run_id,
                data={"tool": name, "error": reason},
            ))
            raise ToolPolicyError(f"执行预算超限: {reason}")

        executor = ToolExecutor(self.ctx.db_info.get("db") if self.ctx.db_info else None, self.ctx.agent_config)
        # db / agent handler 需要真实 db session：由 service 注入
        executor.db = self.ctx.db_info.get("session") if self.ctx.db_info else None

        try:
            result = await executor.execute(name, args)
        except Exception as e:  # 工具异常不吞，显式失败
            duration_ms = (time.monotonic() - started) * 1000
            await self.bus.publish(RuntimeEvent(
                type=TOOL_FAILED,
                run_id=self.ctx.run_id,
                data={"tool": name, "error": str(e), "duration_ms": round(duration_ms, 1)},
            ))
            telemetry.record_tool(
                self.ctx.to_log_fields(), tool=name, status="failed",
                duration_ms=round(duration_ms, 1), error=str(e),
            )
            return f"工具执行错误: {e}"

        duration_ms = (time.monotonic() - started) * 1000
        text = result if isinstance(result, str) else str(result)
        telemetry.record_tool(
            self.ctx.to_log_fields(), tool=name, status="completed",
            duration_ms=round(duration_ms, 1),
        )
        await self.bus.publish(RuntimeEvent(
            type=TOOL_COMPLETED,
            run_id=self.ctx.run_id,
            data={
                "tool": name,
                "output": text,
                "duration_ms": round(duration_ms, 1),
            },
        ))
        return text

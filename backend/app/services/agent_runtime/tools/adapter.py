"""
业务工具桥接：BUILTIN_TOOLS 注册表 -> LangChain StructuredTool。

- 由 tools 注册表的 parameters/required 动态生成 pydantic 参数模型
- 执行前过 ToolPolicy 授权/SSRF/路径/沙箱检查
- 执行经原 ToolExecutor 路由（SYNC/ASYNC/DB/AGENT + 自定义 HTTP）
- 工具生命周期事件写入 run 级 EventBus

Phase 3（FR-05）Tool Execution Ledger 接入：
- InjectedToolCallId 注入稳定 tool_call_id（禁止 args_hash 兼当调用 ID）
- 副作用先登记（prepared）-> 执行（executing）-> 确认（succeeded/failed/unknown）
- 崩溃重放治理：succeeded 返回缓存结果；结果未知时按副作用等级决策
  （read_only 重试 / external_idempotent 复用同一幂等键重试 / 其余禁止重放）
- HTTP POST 注入 Idempotency-Key（run_id + tool_call_id 摘要），重试复用同一键
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Annotated, Any, Callable, Coroutine, Dict, List, Optional, Type

from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel, Field, create_model

from app.core.database import AsyncSessionLocal
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
from ..lifecycle import RunExecutionLease
from ..repositories import PersistenceError, ToolExecutionRepository
from ..repositories.errors import RunLeaseLost
from ..telemetry import telemetry
from .classification import (
    CLASS_READ_ONLY,
    EXTERNAL_IDEMPOTENT_TOOLS,
    REPLAY_BLOCK,
    REPLAY_CACHED,
    replay_action,
    tool_outcome_class,
)
from .policy import BudgetExceededError, ToolPolicy, ToolPolicyError

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

# 本地确定性错误（未触达外部副作用）-> failed；其余异常对副作用工具按结果未知处理
_DEFINITIVE_ERRORS = (ToolPolicyError, TypeError, KeyError, ValueError, LookupError, ArithmeticError)

_RESULT_PAYLOAD_LIMIT = 8000


class ToolLedgerError(PersistenceError, GraphBubbleUp):
    """工具执行账本持久化失败：中止本次 Run。

    继承 GraphBubbleUp 以穿透 ToolNode 的通用异常吞噬（ToolNode 对
    GraphBubbleUp 一律重新抛出），保证关键持久化失败不会被降级为
    工具错误消息继续带病执行（R-12 / 任务书 §0.7）。
    """


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
    # 稳定工具调用身份：LangGraph ToolNode 运行时注入（对模型 schema 隐藏）
    fields["tool_call_id"] = (
        Annotated[Optional[str], InjectedToolCallId],
        Field(default=None, description="工具调用 ID（运行时注入）"),
    )
    model = create_model(f"{_camel(name)}Args", **fields)
    return model


def _camel(s: str) -> str:
    return "".join(p.title() for p in s.replace("-", "_").split("_"))


def _args_hash(args: Dict[str, Any]) -> str:
    canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_key(run_id: str, tool_call_id: str) -> str:
    """稳定外部幂等键：run_id + tool_call_id 的摘要（重试复用同一键）。"""
    return hashlib.sha256(f"{run_id}:{tool_call_id}".encode("utf-8")).hexdigest()


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
        args = dict(args)
        # 稳定调用身份（InjectedToolCallId 运行时注入；直接调用时兜底生成）
        tool_call_id = args.pop("tool_call_id", None) or uuid.uuid4().hex
        idempotency_key = _idempotency_key(self.ctx.run_id, tool_call_id)

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

        # 2) 执行意图先持久化（prepared），再执行副作用
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
            raise BudgetExceededError(f"执行预算超限: {reason}")

        rec, created = await self._ledger_begin(
            name=name,
            tool_call_id=tool_call_id,
            args_hash=_args_hash(args),
            args_snapshot=redact_sensitive(args),
            idempotency_key=idempotency_key,
        )

        if not created:
            action = replay_action(rec.status, name)
            if action == REPLAY_CACHED:
                return await self._replay_cached(name, rec, started)
            if action == REPLAY_BLOCK:
                await self._ledger_mark(
                    rec.id, "needs_reconciliation",
                    error="结果未知，禁止自动重放，等待人工核对",
                )
                return await self._refuse_replay(name, rec, started)
            # REPLAY_RETRY：确定失败 / read_only / 幂等键下游，允许带同一幂等键重试

        # 3) 取得执行权：prepared/failed/executing/unknown -> executing（原子转移）
        if not await self._ledger_mark_executing(rec.id):
            # 并发恢复/人工核对刚变更了状态：按结果未知处理，不执行
            fresh, _ = await self._ledger_begin(
                name=name,
                tool_call_id=tool_call_id,
                args_hash=_args_hash(args),
                args_snapshot=redact_sensitive(args),
                idempotency_key=idempotency_key,
            )
            return await self._refuse_replay(name, fresh, started)

        # 4) 外部副作用注入稳定幂等键（重试复用同一键，下游按 Idempotency-Key 去重）
        if name in EXTERNAL_IDEMPOTENT_TOOLS:
            headers = dict(args.get("headers") or {})
            headers.setdefault("Idempotency-Key", idempotency_key)
            args["headers"] = headers

        executor = ToolExecutor(self.ctx.db_info.get("db") if self.ctx.db_info else None, self.ctx.agent_config)
        # db / agent handler 需要真实 db session：由 service 注入
        executor.db = self.ctx.db_info.get("session") if self.ctx.db_info else None

        try:
            result = await executor.execute(name, args, raise_on_error=True)
        except Exception as e:  # 工具异常不吞，显式失败
            duration_ms = (time.monotonic() - started) * 1000
            if tool_outcome_class(name) != CLASS_READ_ONLY and not isinstance(e, _DEFINITIVE_ERRORS):
                # 副作用工具遇到不确定异常（超时/连接中断等）：远端可能已成功，
                # 标记 unknown 禁止自动重放（F-07/F-08），绝不谎报成功或失败
                await self._ledger_mark(rec.id, "unknown", error=str(e))
            else:
                await self._ledger_mark(rec.id, "failed", error=str(e))
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
        # 结果确认必须先于对外宣告（关键写入失败向上抛出，不重复执行副作用）
        await self._ledger_mark(rec.id, "succeeded", result_payload=text[:_RESULT_PAYLOAD_LIMIT])
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

    # ── 崩溃重放分支 ─────────────────────────────────────────────────────

    async def _replay_cached(self, name: str, rec: Any, started: float) -> str:
        """T-04：已 succeeded 的调用返回持久化结果，绝不重复执行。"""
        payload = rec.result_payload
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        duration_ms = (time.monotonic() - started) * 1000
        await self.bus.publish(RuntimeEvent(
            type=TOOL_COMPLETED,
            run_id=self.ctx.run_id,
            data={
                "tool": name,
                "output": text,
                "duration_ms": round(duration_ms, 1),
                "replay": True,
            },
        ))
        return text

    async def _refuse_replay(self, name: str, rec: Any, started: float) -> str:
        """T-05：结果未知的调用禁止盲目重放，返回核对提示。"""
        duration_ms = (time.monotonic() - started) * 1000
        text = (
            f"工具 {name} 的上次执行结果未知（{rec.status}），"
            "无法确认远端是否已产生副作用，已阻止自动重放。"
            "请勿再次发起同一操作，需人工核对确认后重试。"
        )
        await self.bus.publish(RuntimeEvent(
            type=TOOL_FAILED,
            run_id=self.ctx.run_id,
            data={"tool": name, "error": text, "duration_ms": round(duration_ms, 1)},
        ))
        telemetry.record_tool(
            self.ctx.to_log_fields(), tool=name, status="blocked_replay", error=text,
        )
        return text

    # ── Tool Execution Ledger（关键写入失败一律向上抛出） ─────────────────

    async def _ledger_begin(
        self,
        *,
        name: str,
        tool_call_id: str,
        args_hash: str,
        args_snapshot: Dict[str, Any],
        idempotency_key: str,
    ):
        return await self._ledger_op(lambda repo: repo.begin(
            run_id=self.ctx.run_id,
            tool_call_id=tool_call_id,
            tool_name=name,
            args_hash=args_hash,
            args_snapshot=args_snapshot,
            idempotency_key=idempotency_key,
        ))

    def _execution_lease(self) -> Optional[RunExecutionLease]:
        """当前执行权身份（P0-02 LF-07）：Ledger 写入随带并由仓库校验。"""
        if self.ctx.worker_id and self.ctx.lease_version:
            return RunExecutionLease(
                run_id=self.ctx.run_id,
                worker_id=self.ctx.worker_id,
                lease_version=self.ctx.lease_version,
            )
        return None

    async def _ledger_mark_executing(self, exec_id: int) -> bool:
        return await self._ledger_op(lambda repo: repo.mark_executing(
            exec_id,
            worker_id=self.ctx.worker_id,
            lease_version=self.ctx.lease_version,
            lease=self._execution_lease(),
        ))

    async def _ledger_mark(
        self,
        exec_id: int,
        status: str,
        *,
        error: Optional[str] = None,
        result_payload: Optional[Any] = None,
    ) -> bool:
        lease = self._execution_lease()

        async def op(repo: ToolExecutionRepository) -> bool:
            if status == "succeeded":
                return await repo.mark_succeeded(
                    exec_id, result_payload=result_payload, lease=lease
                )
            if status == "failed":
                return await repo.mark_failed(exec_id, error=error, lease=lease)
            if status == "unknown":
                return await repo.mark_unknown(exec_id, error=error, lease=lease)
            return await repo.mark_needs_reconciliation(exec_id, error=error, lease=lease)

        return await self._ledger_op(op)

    @staticmethod
    async def _ledger_op(
        op: Callable[[ToolExecutionRepository], Coroutine[Any, Any, Any]]
    ) -> Any:
        try:
            async with AsyncSessionLocal() as db:
                repo = ToolExecutionRepository(db)
                result = await op(repo)
                await db.commit()
                return result
        except (PersistenceError, RunLeaseLost):
            raise
        except Exception as e:
            raise ToolLedgerError(f"工具执行账本写入失败: {e}") from e

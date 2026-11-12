"""
业务工具桥接：BUILTIN_TOOLS 注册表 -> LangChain StructuredTool。

- 由 tools 注册表的 parameters/required 动态生成 pydantic 参数模型
- 执行前过 ToolPolicy 授权/SSRF/路径/沙箱检查
- 执行经原 ToolExecutor 路由（SYNC/ASYNC/DB/AGENT + 自定义 HTTP）
- 工具生命周期事件写入 run 级 EventBus

Phase 3（FR-05 / P0-03）Tool Execution Ledger 接入：
- InjectedToolCallId 注入稳定 tool_call_id（禁止 args_hash 兼当调用 ID）
- 副作用先登记（prepared）-> 执行（executing）-> 确认（succeeded/failed/unknown）
- 全部账本写入携带 RunExecutionLease 无条件校验执行权（§5.2 A）；
  begin 命中已有记录必须比对 tool_name/args_hash/idempotency_key，
  任何不一致抛 ToolCallIdentityConflict（§5.2 B）
- 崩溃重放治理：succeeded 返回缓存结果；结果未知时按副作用等级决策
  （read_only 重试 / external_idempotent 复用同一幂等键重试 / 其余禁止重放）；
  重入执行（非 prepared 源状态）唯一来源是 replay_action == REPLAY_RETRY
  的副作用等级判定（reentry=True，§5.2 C）
- 账本写入失败/状态迁移被拒必须被 Adapter 感知：抛异常停止 Graph，
  不发布 TOOL_COMPLETED，不降级为普通工具错误（§5.2 D）
- HTTP POST 注入 Idempotency-Key（run_id + tool_call_id 摘要），重试复用同一键

Phase 5（P0-05）统一 ToolResult（§7.2）：
- executor.execute 结果统一归一化为 ToolResult（str 兜底 succeeded）
- succeeded -> mark_succeeded + TOOL_COMPLETED；
  failed -> mark_failed + TOOL_FAILED（不得伪装成功）；
  unknown -> mark_unknown + TOOL_FAILED（结果未知，禁止重复发起）
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
from app.services.tools.result import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    ToolResult,
    normalize,
)

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
from ..repositories.errors import IllegalTransition, RunLeaseLost
from ..repositories.tool_execution_repository import (
    TOOL_EXEC_NEEDS_RECONCILIATION,
    TOOL_EXEC_PREPARED,
    TOOL_EXEC_SUCCEEDED,
)
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

        # 2) 工具生命周期开始 + 执行预算记账（FR-10.7：预算超限显式终止）
        await self.bus.publish(RuntimeEvent(
            type=TOOL_STARTED,
            run_id=self.ctx.run_id,
            data={"tool": name, "input": redact_sensitive(args)},
        ))
        self.ctx.tool_calls += 1
        reason = self.ctx.budget.exhausted_reason(
            self.ctx.steps, self.ctx.tool_calls, self.ctx.model_calls
        )
        if reason:
            await self.bus.publish(RuntimeEvent(
                type=TOOL_FAILED,
                run_id=self.ctx.run_id,
                data={"tool": name, "error": reason},
            ))
            raise BudgetExceededError(f"执行预算超限: {reason}")

        # 3) 账本登记：prepared（执行权无条件校验 + 调用身份一致性）
        #    同 (run_id, tool_call_id) 已有记录且 tool_name/args_hash/idempotency_key
        #    不一致时抛 ToolCallIdentityConflict 显式中止（§5.2 B）
        started = time.monotonic()
        rec, created = await self._ledger_begin(
            run_id=self.ctx.run_id,
            tool_call_id=tool_call_id,
            tool_name=name,
            args_hash=_args_hash(args),
            args_snapshot=redact_sensitive(dict(args)),
            idempotency_key=idempotency_key,
        )

        if not created:
            # 崩溃恢复重放决策（副作用等级判定，classification.py）
            action = replay_action(rec.status, name)
            if action == REPLAY_CACHED:
                return await self._replay_cached(name, rec, started)
            if action == REPLAY_BLOCK:
                # 结果未知/人工核对中：禁止自动重放，转入 needs_reconciliation 供核对
                if rec.status != TOOL_EXEC_NEEDS_RECONCILIATION:
                    await self._ledger_mark(
                        rec.id,
                        "needs_reconciliation",
                        error=f"恢复发现结果未知（{rec.status}），禁止自动重放",
                    )
                return await self._refuse_replay(name, rec, started)
            # REPLAY_RETRY：进入重入执行判定

        # 4) 进入执行态：prepared->executing 是一次性原子翻转（并发唯一执行者）；
        #    executing/failed/unknown 残留重入必须 reentry=True（副作用等级判定，
        #    唯一来源是上面的 replay_action == REPLAY_RETRY 分支，§5.2 C）
        reentry = rec.status != TOOL_EXEC_PREPARED
        if not await self._ledger_mark_executing(rec.id, reentry=reentry):
            # 竞态落败（并发同一 tool_call_id / 状态刚被迁移）：不执行、优雅拒绝
            fresh, _ = await self._ledger_begin(
                run_id=self.ctx.run_id,
                tool_call_id=tool_call_id,
                tool_name=name,
                args_hash=_args_hash(args),
                args_snapshot=redact_sensitive(dict(args)),
                idempotency_key=idempotency_key,
            )
            if fresh.status == TOOL_EXEC_SUCCEEDED:
                return await self._replay_cached(name, fresh, started)
            return await self._refuse_replay(name, fresh, started)

        # 5) 外部副作用注入稳定幂等键（重试复用同一键，下游按 Idempotency-Key 去重）
        if name in EXTERNAL_IDEMPOTENT_TOOLS:
            headers = dict(args.get("headers") or {})
            headers.setdefault("Idempotency-Key", idempotency_key)
            args["headers"] = headers

        # 6) 执行（在非 async 线程池跑 sync handler，不阻塞事件循环）
        try:
            executor = ToolExecutor(
                self.ctx.db_info.get("db") if self.ctx.db_info else None,
                self.ctx.agent_config,
            )
            # db / agent handler 需要真实 db session：由 service 注入
            executor.db = self.ctx.db_info.get("session") if self.ctx.db_info else None
            result = await executor.execute(name, args, raise_on_error=True)
            tr: ToolResult = normalize(result)
            duration_ms = int((time.monotonic() - started) * 1000)

            if tr.status == STATUS_SUCCEEDED:
                # 远端/本地已确认成功：只有账本确认写入成功后才宣告成功（§5.2 D）
                text = tr.text
                await self._ledger_mark(
                    rec.id,
                    "succeeded",
                    result_payload=text[:_RESULT_PAYLOAD_LIMIT],
                    external_operation_id=tr.external_operation_id,
                )
                await self.bus.publish(RuntimeEvent(
                    type=TOOL_COMPLETED,
                    run_id=self.ctx.run_id,
                    data={"tool": name, "output": text[:2000]},
                ))
                telemetry.record_tool(
                    self.ctx.to_log_fields(), tool=name, status="completed",
                    duration_ms=float(duration_ms),
                )
                return text

            if tr.status == STATUS_FAILED:
                # 业务错误不得伪装成功字符串（§7.2 C）：标记 failed 并回传失败文本
                err_text = (tr.error or tr.text)[:_RESULT_PAYLOAD_LIMIT]
                await self._ledger_mark(rec.id, "failed", error=err_text)
                await self.bus.publish(RuntimeEvent(
                    type=TOOL_FAILED,
                    run_id=self.ctx.run_id,
                    data={"tool": name, "error": err_text[:500]},
                ))
                telemetry.record_tool(
                    self.ctx.to_log_fields(), tool=name, status="failed",
                    duration_ms=float(duration_ms), error=err_text[:500],
                )
                return tr.text

            # STATUS_UNKNOWN：无法确认外部操作是否已生效（§7.2 A）
            err_text = (tr.error or tr.text)[:_RESULT_PAYLOAD_LIMIT]
            await self._ledger_mark(rec.id, "unknown", error=err_text)
            await self.bus.publish(RuntimeEvent(
                type=TOOL_FAILED,
                run_id=self.ctx.run_id,
                data={"tool": name, "error": err_text[:500]},
            ))
            telemetry.record_tool(
                self.ctx.to_log_fields(), tool=name, status="unknown",
                duration_ms=float(duration_ms), error=err_text[:500],
            )
            return (
                f"工具 {name} 执行结果未知：{err_text[:500]}。"
                "无法确认远端操作是否已生效，禁止重复发起同一操作，需人工核对。"
            )

        except (PersistenceError, RunLeaseLost, IllegalTransition):
            # 账本持久化 / 执行权 / 状态机异常必须被 Adapter 感知（§5.2 D）：
            # 原样冒泡停止 Graph，绝不降级为普通工具错误文本继续带病执行
            raise
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            is_definitive = isinstance(e, _DEFINITIVE_ERRORS)
            status = (
                "failed" if is_definitive or tool_outcome_class(name) == CLASS_READ_ONLY else "unknown"
            )
            # 账本写入失败时 ToolLedgerError 会向上冒泡，不会走到 return
            await self._ledger_mark(rec.id, status, error=str(e))
            await self.bus.publish(RuntimeEvent(
                type=TOOL_FAILED,
                run_id=self.ctx.run_id,
                data={"tool": name, "error": str(e)[:500]},
            ))
            telemetry.record_tool(
                self.ctx.to_log_fields(), tool=name, status=status,
                duration_ms=float(duration_ms), error=str(e)[:500],
            )
            return f"工具执行错误: {e}"

    async def _replay_cached(self, name: str, rec, started: float) -> str:
        """FR-05：已 succeeded 的调用返回已存结果，不重复执行。"""
        payload = rec.result_payload
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, default=str
        )
        await self.bus.publish(RuntimeEvent(
            type=TOOL_COMPLETED,
            run_id=self.ctx.run_id,
            data={"tool": name, "output": text[:2000], "replay": "cached"},
        ))
        telemetry.record_tool(self.ctx.to_log_fields(), tool=name, status="cached", duration_ms=0.0)
        return text

    async def _refuse_replay(self, name: str, rec, started: float) -> str:
        """结果未知/禁止重放：绝不重复发起副作用，交由恢复流程/人工核对。"""
        await self.bus.publish(RuntimeEvent(
            type=TOOL_FAILED,
            run_id=self.ctx.run_id,
            data={
                "tool": name,
                "error": f"结果未知（{rec.status}），阻止自动重放",
            },
        ))
        telemetry.record_tool(self.ctx.to_log_fields(), tool=name, status="refused", duration_ms=0.0)
        return (
            f"工具 {name} 上次执行结果未知（{rec.status}），"
            f"为避免重复副作用已阻止自动重放，请人工核对后再决定。"
        )

    # ── Ledger bridge（run_id/tool_call_id 稳定身份 + 持久化失败必须被感知）──

    def _execution_lease(self) -> RunExecutionLease:
        """执行权身份（§5.2 A）：Ledger 全部写入的强制校验依据。

        ctx 缺少 worker_id / lease_version 时不得假装拥有执行权，
        直接抛 ToolLedgerError 拒绝写入（不降级为普通工具错误）。
        """
        if not self.ctx.worker_id or not self.ctx.lease_version:
            raise ToolLedgerError(
                f"run {self.ctx.run_id} 缺少执行权身份（worker_id/lease_version），"
                "拒绝 Tool Ledger 写入"
            )
        return RunExecutionLease(
            run_id=self.ctx.run_id,
            worker_id=self.ctx.worker_id,
            lease_version=int(self.ctx.lease_version),
        )

    async def _ledger_op(
        self,
        op: Callable[[Any], Coroutine[Any, Any, Any]],
    ) -> Any:
        """账本操作执行器：执行权校验与条件写入在同一事务内提交。

        持久化失败必须被 Adapter 感知（§5.2 D）：
        - RunLeaseLost / IllegalTransition / ToolCallIdentityConflict 原样抛出
          （均为 GraphBubbleUp，穿透 ToolNode 吞噬，不得降级为普通工具错误）；
        - 其他数据库异常包装为 ToolLedgerError 中止 Graph；
        - 账本未提交不得宣告成功（succeeded 确认写入失败即抛异常）。
        """
        async with AsyncSessionLocal() as s:
            try:
                result = await op(s)
                await s.commit()
                return result
            except (PersistenceError, RunLeaseLost, IllegalTransition):
                raise
            except Exception as e:
                await s.rollback()
                raise ToolLedgerError(f"工具执行账本写入失败: {e}") from e

    async def _ledger_begin(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        args_hash: Optional[str],
        args_snapshot: Optional[Dict[str, Any]],
        idempotency_key: Optional[str],
    ):
        lease = self._execution_lease()

        async def _op(s):
            return await ToolExecutionRepository(s).begin(
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args_hash=args_hash,
                args_snapshot=args_snapshot,
                idempotency_key=idempotency_key,
                lease=lease,
            )

        return await self._ledger_op(_op)

    async def _ledger_mark_executing(self, exec_id: int, *, reentry: bool = False) -> bool:
        lease = self._execution_lease()

        async def _op(s):
            return await ToolExecutionRepository(s).mark_executing(
                exec_id, lease=lease, reentry=reentry
            )

        return await self._ledger_op(_op)

    async def _ledger_mark(
        self,
        exec_id: int,
        target: str,
        *,
        error: Optional[str] = None,
        result_payload: Optional[Any] = None,
        external_operation_id: Optional[str] = None,
    ) -> bool:
        lease = self._execution_lease()

        async def _op(s):
            repo = ToolExecutionRepository(s)
            if target == "succeeded":
                return await repo.mark_succeeded(
                    exec_id,
                    result_payload=result_payload,
                    external_operation_id=external_operation_id,
                    lease=lease,
                )
            if target == "failed":
                return await repo.mark_failed(exec_id, error=error, lease=lease)
            if target == "unknown":
                return await repo.mark_unknown(exec_id, error=error, lease=lease)
            return await repo.mark_needs_reconciliation(exec_id, error=error, lease=lease)

        return await self._ledger_op(_op)

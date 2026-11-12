"""
AgentRuntimeService：平台唯一 Agent 执行入口（DeepAgents + LangGraph）。

职责：
- 创建/驱动 Run（DB pending -> Worker CAS Claim -> 执行 + 事件总线）
- 模型/工具/预算/安全策略组装（factory / tools / model_adapter）
- Checkpoint 持久化与 thread 管理（persistence）
- HITL 中断→审批→恢复（interrupt / Command(resume)）
- 取消、超时、预算超限的确定性状态
- Run/Approval 记录落库（agent_runs / agent_approvals），关键状态写入失败向上抛出
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from app.core.config import settings
from app.models.agent import AgentRun as AgentRunRow, AgentSession
from app.core.database import AsyncSessionLocal

from .context import (
    ExecutionBudget,
    ModelUsageCallback,
    RuntimeContext,
    RunScope,
    messages_to_langchain,
)
from .events import (
    APPROVAL_REQUIRED,
    MODEL_DELTA,
    PLAN_UPDATED,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    RUN_SUSPENDED,
    RUN_WAITING_APPROVAL,
    SUBAGENT_COMPLETED,
    SUBAGENT_STARTED,
    TOOL_COMPLETED,
    TERMINAL_EVENTS,
    EventBus,
    LegacyEventAdapter,
    RuntimeEvent,
    redact_sensitive,
)
from .factory import build_agent
from .lifecycle import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_LIMIT_EXCEEDED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    TERMINAL_STATUSES,
    InvalidTransition,
    RunExecutionLease,
    RunHandle,
    RunRegistry,
)
from .model_adapter import ModelCapabilityError
from .persistence import CheckpointerFactory, make_run_config, make_thread_id
from .repositories import (
    EventRepository,
    IdempotencyConflict,
    OUTBOX_DELIVERED,
    PersistenceError,
    RunRepository,
    SessionOutboxRepository,
    SessionRepository,
    ToolExecutionRepository,
)
from .repositories.errors import IllegalTransition, RunLeaseLost, SessionBusy
from .telemetry import telemetry
from .tools.classification import (
    CHECKPOINT_MISSING,
    CHECKPOINT_OK,
    CHECKPOINT_UNREADABLE,
    recovery_decision,
)
from .tools.policy import BudgetExceededError, ToolPolicy, ToolPolicyError
from .worker import (
    RECOVERY_CONTINUE,
    RECOVERY_FRESH,
    RECOVERY_NEEDS_RECONCILIATION,
    RECOVERY_WAITING_APPROVAL,
    RunJob,
    RunWorker,
)

logger = logging.getLogger("agent_runtime")


# ── 审批决策异常（FR-04：冲突/非法决策显式拒绝，API 层可映射 HTTP 4xx） ──


class ApprovalInvalid(Exception):
    """审批决策非法（未知决策类型 / 未知审批动作 / 运行不存在）。"""
    code = "invalid"


class ApprovalConflict(Exception):
    """审批决策与既有决策冲突：同一动作不允许改判（F-11）。"""
    code = "conflict"


class ApprovalForbidden(Exception):
    """无权审批该运行。"""
    code = "forbidden"


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_text(content: Any) -> str:
    """FR-09.5：模型内容文本提取规则——str 原样返回；结构化/多模态 block 列表
    按 text 块拼接（含纯字符串块），不静默丢弃可保留的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _action_from_req(req: Dict[str, Any], interrupt_id: str, idx: int) -> Dict[str, Any]:
    """从 HITL interrupt 的 action_request 构造稳定审批动作（FR-06）。

    action_id 优先取 tool_call_id；否则由 interrupt_id + 序号 + 工具 + 参数
    摘要确定 —— 重启后从 Checkpoint 重提取时保持同一 ID，天然幂等。
    """
    raw_args = dict(req.get("args") or {})
    tool = str(req.get("name") or req.get("action") or req.get("tool") or "unknown")
    args_hash = hashlib.sha256(
        json.dumps(raw_args, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    action_id = str(req.get("tool_call_id") or hashlib.sha256(
        f"{interrupt_id}|{idx}|{tool}|{args_hash}".encode("utf-8")
    ).hexdigest()[:40])
    return {
        "action_id": action_id,
        "tool_call_id": req.get("tool_call_id"),
        "tool": tool,
        "args": redact_sensitive(raw_args),
        "args_hash": args_hash,
        "description": req.get("description", ""),
    }


def _synthetic_terminal_event(run_row: Any) -> RuntimeEvent:
    """无事件日志的旧运行：按状态合成等价终态事件（FR-07 回放兼容）。"""
    if run_row.status == STATUS_WAITING_APPROVAL:
        return RuntimeEvent(type=RUN_WAITING_APPROVAL, run_id=run_row.id, data={})
    if run_row.status == STATUS_COMPLETED:
        return RuntimeEvent(
            type=RUN_COMPLETED,
            run_id=run_row.id,
            data={"final_text": run_row.final_text or ""},
        )
    if run_row.status == STATUS_CANCELLED:
        return RuntimeEvent(type=RUN_CANCELLED, run_id=run_row.id, data={})
    return RuntimeEvent(
        type=RUN_FAILED,
        run_id=run_row.id,
        data={"error": run_row.error or "运行失败", "status": run_row.status},
    )


class AgentRuntimeService:
    """统一 Agent 执行服务（单例使用）。"""

    def __init__(self) -> None:
        self.registry = RunRegistry.instance()
        self.worker: Optional[RunWorker] = RunWorker(self)

    # ── 公共 API ──────────────────────────────────────────────────────────

    async def start_run(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, AsyncGenerator[Dict[str, Any], None]]:
        """启动运行并返回 (run_id, 事件流)：run_id 可先行写入响应头（X-Run-Id）。"""
        run, _adapter, stream = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
            endpoint=endpoint,
            user_message=user_message,
        )
        return run.run_id, stream

    async def start_run_with_session(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Optional[int], AsyncGenerator[Dict[str, Any], None]]:
        """启动运行并返回 (run_id, session_id, 事件流)。

        session_id 为业务事务内解析后的会话 ID（未指定时由服务创建），
        供 API 写入 X-Session-Id 响应头。
        """
        run, _adapter, stream = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
            endpoint=endpoint,
            user_message=user_message,
        )
        return run.run_id, run.session_id, stream

    async def lookup_idempotent(
        self,
        user_id: Optional[int],
        idempotency_key: str,
        request_hash: str,
    ):
        """幂等预查（P1-01：收敛到 Service/Repository，API 不再裸查）。

        - 同 Key 同请求摘要：返回已有 Run 行（调用方复用，不重复执行/追加消息）；
        - 同 Key 不同请求摘要：抛 ``IdempotencyConflict``（API 映射 HTTP 409）。
        """
        async with AsyncSessionLocal() as db:
            existing = await RunRepository(db).get_by_idempotency_key(
                user_id, idempotency_key
            )
            if existing is None:
                return None
            return RunRepository._check_reuse(existing, request_hash)

    async def idempotent_replay_stream(
        self, run_id: str
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """幂等命中的 SSE 回放流（P1-01）：按持久化事件重建旧契约事件。

        流式请求重试命中幂等时必须保持 SSE 协议（FR-01），不得改为普通
        JSON；回放以 ``done`` 终止（运行未终结时按当前状态合成终止事件，
        后续进度可经 /runs/{run_id}/events 获取）。
        """
        ended = False
        async for legacy in self.replay_events(run_id):
            ended = ended or legacy.get("type") == "done"
            yield legacy
        if not ended:
            async with AsyncSessionLocal() as db:
                row = await RunRepository(db).get_run(run_id)
            yield {"type": "done", "status": row.status if row is not None else "failed"}

    async def stream_run(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """旧 SSE 契约事件流（data: {json} 扁平事件）。"""
        _, stream = await self.start_run(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
        )
        async for legacy in stream:
            yield legacy

    async def run_to_completion(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """非流式执行：返回 {status, content, steps, run_id, session_id}。"""
        run, _adapter, stream = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
            endpoint=endpoint,
            user_message=user_message,
        )
        steps: List[Dict[str, Any]] = []
        content = ""
        status = run.status
        async for legacy in stream:
            if legacy.get("type") == "final":
                content = legacy.get("content") or ""
            if legacy.get("type") == "done":
                status = legacy.get("status") or status
                continue
            steps.append(legacy)
        return {
            "status": status,
            "content": content,
            "steps": steps,
            "run_id": run.run_id,
            "session_id": run.session_id,
        }

    async def resume_run(
        self,
        run_id: str,
        decisions: List[Dict[str, Any]],
        deciding_user_id: int,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """HITL 恢复：decisions=[{"type":"approve"|"reject","message":...,"action_id":...}, ...]。

        幂等语义（F-11 / T-03）：重复提交一致决策返回当前状态、不重复恢复；
        冲突决策（ApprovalConflict）与非法决策（ApprovalInvalid）显式拒绝。
        服务重启后无内存句柄也可恢复：以 agent_runs / Checkpoint 为准。
        """
        run = self.registry.get(run_id)
        if run is None:
            async with AsyncSessionLocal() as db:
                row = await RunRepository(db).get_run(run_id)
            if row is None:
                yield {"type": "error", "content": "运行不存在或不在待审批状态"}
                yield {"type": "done", "status": "failed"}
                return
            run = self.registry.attach(
                run_id=row.id,
                agent_id=row.agent_id,
                session_id=row.session_id,
                user_id=row.user_id,
                thread_id=row.thread_id,
                idempotency_key=row.idempotency_key,
                status=row.status,
            )
        if run.user_id not in (None, deciding_user_id):
            yield {"type": "error", "content": "无权审批该运行", "code": "forbidden"}
            yield {"type": "done", "status": "failed"}
            return

        # 决策校验：非法/冲突显式拒绝；重复一致提交幂等
        try:
            verdict = await self._check_decisions(run_id, decisions)
        except (ApprovalInvalid, ApprovalConflict) as e:
            yield {"type": "error", "content": str(e), "code": e.code}
            yield {"type": "done", "status": "failed"}
            return

        if run.status != STATUS_WAITING_APPROVAL:
            if verdict == "idempotent":
                # F-11 / T-03：重复审批幂等返回当前状态，不重复恢复、不重复执行
                yield {"type": "done", "status": run.status, "idempotent": True, "run_id": run_id}
            else:
                yield {"type": "error", "content": "运行不存在或不在待审批状态"}
                yield {"type": "done", "status": "failed"}
            return

        # 审批决策必须先持久化成功（关键写入失败向上抛出，不得带病执行副作用）
        await self._record_approval_decision(run_id, decisions, deciding_user_id)

        # 图内恢复用决策：按审批登记顺序对齐 action_requests（与 Checkpoint 一致）
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AgentApproval).where(AgentApproval.run_id == run_id).order_by(AgentApproval.id)
            )).scalars().all()
        graph_decisions = [
            {"type": r.decision, "message": r.message or ""}
            for r in rows if r.decision is not None
        ]

        # 原子领取（waiting_approval -> running + 租约 + fencing token）
        assert self.worker is not None
        row = await self.worker.claim_run(run_id, paused=True)
        if row is None:
            yield {"type": "error", "content": "运行状态已变更或已被其他执行者接管"}
            yield {"type": "done", "status": "failed"}
            return

        adapter = LegacyEventAdapter(run_id)
        run.lease_version = row.lease_version
        run.worker_id = self.worker.worker_id
        run.lease_expires_at = row.lease_expires_at
        run.attempt = row.attempt or run.attempt
        run.transition(STATUS_RUNNING)

        run.bus = EventBus(persist=True)
        run.bus.bind_run(thread_id=run.thread_id, agent_id=run.agent_id, trace_id=run.run_id)
        self.worker.supervise(
            run,
            adapter,
            RunJob(
                run_id=run_id,
                user_id=run.user_id,
                resume={"decisions": graph_decisions},
                use_memory_context="",
            ),
        )
        async for legacy in self._bus_stream(run, adapter):
            yield legacy

    async def cancel_run(self, run_id: str, user_id: int) -> Dict[str, Any]:
        run = self.registry.get(run_id)
        if run is None:
            # 无内存句柄：按数据库状态取消（pending 走无 Lease 专用 CAS）
            async with AsyncSessionLocal() as db:
                repo = RunRepository(db)
                row = await repo.get_run(run_id)
                if row is None or row.user_id not in (None, user_id):
                    return {"ok": False, "error": "运行不存在"}
                if row.status in TERMINAL_STATUSES:
                    return {"ok": True, "status": row.status}
                if row.status == STATUS_PENDING:
                    ok = await repo.cancel_without_lease(run_id)
                    if ok and row.session_id:
                        # 无 Lease 取消也要释放 Session 执行权（P1-02）
                        await SessionRepository(db).release_execution(
                            row.session_id, run_id
                        )
                    await db.commit()
                    if ok:
                        return {"ok": True, "status": STATUS_CANCELLED}
                    return {"ok": False, "error": "运行状态已变更（可能已被 Worker 领取），无法直接取消"}
            return {"ok": False, "error": "运行执行中由其他 Worker 持有，无法直接取消"}
        if run.user_id != user_id:
            return {"ok": False, "error": "无权取消该运行"}
        if run.is_terminal:
            return {"ok": True, "status": run.status}
        if run.task and not run.task.done():
            # _drive 的 CancelledError 分支负责落库 cancelled（fencing 校验）
            run.task.cancel()
            return {"ok": True, "status": "cancelling"}
        # 无执行权（pending / 等待审批 / 待核对）：无 Lease 专用 CAS（LF-06），
        # 不得用无条件更新覆盖正在执行的 Run
        async with AsyncSessionLocal() as db:
            ok = await RunRepository(db).cancel_without_lease(
                run_id,
                from_statuses=(
                    STATUS_PENDING,
                    STATUS_WAITING_APPROVAL,
                    STATUS_NEEDS_RECONCILIATION,
                ),
            )
            if ok and run.session_id:
                # 无 Lease 取消也要释放 Session 执行权（P1-02）
                await SessionRepository(db).release_execution(
                    run.session_id, run_id
                )
            await db.commit()
        if ok:
            try:
                run.transition(STATUS_CANCELLED)
            except InvalidTransition:
                pass
            return {"ok": True, "status": STATUS_CANCELLED}
        return {"ok": False, "error": "运行状态已变更，无法直接取消"}

    async def get_run(self, run_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        run = self.registry.get(run_id)
        if run is None:
            async with AsyncSessionLocal() as db:
                from sqlalchemy import select
                from app.models.agent import AgentRun

                row = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
                if row is None or (row.user_id not in (None, user_id)):
                    return None
                return {
                    "run_id": row.id,
                    "status": row.status,
                    "agent_id": row.agent_id,
                    "session_id": row.session_id,
                    "thread_id": row.thread_id,
                    "error": row.error,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                }
        if run.user_id != user_id:
            return None
        return {
            "run_id": run.run_id,
            "status": run.status,
            "agent_id": run.agent_id,
            "session_id": run.session_id,
            "thread_id": run.thread_id,
            "error": run.error,
            "pending_actions": redact_sensitive(run.pending_actions),
            "created_at": run.created_at.isoformat(),
        }

    async def get_thread_state(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """查询 LangGraph 线程状态（恢复语义：checkpoint 保存全量执行状态）。"""
        cp = await CheckpointerFactory.get().get_checkpointer()
        config = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = await cp.aget(config)
        except Exception as e:
            return {"error": str(e)}
        if snapshot is None:
            return None
        actions, has_interrupt = self._extract_snapshot_actions(snapshot)
        return {
            "thread_id": thread_id,
            "checkpoint_id": getattr(snapshot, "id", None),
            "next": list(getattr(snapshot, "next", ()) or ()),
            "has_pending_writes": bool(getattr(snapshot, "pending_writes", None)),
            "has_interrupt": has_interrupt,
            "pending_actions": actions,
        }

    @staticmethod
    def _extract_snapshot_actions(snapshot: Any) -> Tuple[List[Dict[str, Any]], bool]:
        """从 Checkpoint 快照提取未决 HITL 动作（恢复审批所需）。

        动作带稳定 action_id（FR-06）：重启后重提取与中断时记录同一 ID。
        """
        actions: List[Dict[str, Any]] = []
        has_interrupt = False
        for task in (getattr(snapshot, "tasks", None) or ()):
            for it in (getattr(task, "interrupts", None) or ()):
                has_interrupt = True
                value = _field(it, "value", it)
                interrupt_id = str(_field(it, "id", "") or "")
                if isinstance(value, dict) and "action_requests" in value:
                    for ridx, req in enumerate(value["action_requests"]):
                        actions.append(_action_from_req(req, interrupt_id, ridx))
        return actions, has_interrupt

    # ── 内部：启动与驱动 ──────────────────────────────────────────────────

    async def _start(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int],
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Tuple[RunHandle, LegacyEventAdapter, AsyncGenerator[Dict[str, Any], None]]:
        """创建 Run（DB pending）→ Worker CAS Claim → 执行；返回句柄、适配器与旧契约事件流。

        幂等（P1-01 收敛 Service/Repository）：同一幂等键同一请求摘要返回已有
        Run（绝不重复执行、不重复追加用户消息）；同一幂等键不同请求摘要抛
        IdempotencyConflict（API 映射 409）。新 Session 创建、用户消息追加与
        Run 创建同一事务提交，并发幂等重试不产生孤立 Session。
        """
        snapshot = {k: v for k, v in (agent_config or {}).items() if not k.startswith("_")}
        input_payload = {
            "messages": messages,
            "use_memory_context": use_memory_context,
            "db_info": db_info,
        }

        row, created, resolved_session_id = await self._begin_run(
            agent_config=agent_config,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            request_hash=compute_request_hash(
                endpoint=endpoint,
                user_id=user_id,
                agent_id=agent_config.get("id"),
                session_id=session_id,
                user_input=(user_message or {}).get("content")
                or _last_user_input(messages),
                agent_config=agent_config,
            ),
            input_payload=input_payload,
            snapshot=snapshot,
            user_message=user_message,
        )
        run_id = row.id
        thread_id = row.thread_id

        run = self.registry.attach(
            run_id=run_id,
            agent_id=agent_config["id"],
            session_id=resolved_session_id,
            user_id=user_id,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            status=STATUS_PENDING if created else row.status,
        )
        adapter = LegacyEventAdapter(run_id)

        if not created:
            # 幂等命中：绝不重复创建/重复执行
            if run.task is not None and not run.task.done():
                return run, adapter, self._bus_stream(run, adapter)
            return run, adapter, self._replay_db_stream(run_id, run, adapter)

        assert self.worker is not None
        claimed = await self.worker.claim_run(run_id)
        if claimed is None:
            # 极小概率被其他 Worker 先行领取（或 Session 执行权被占）：
            # 退化为数据库回放流，不重复执行
            return run, adapter, self._replay_db_stream(run_id, run, adapter)

        run.lease_version = claimed.lease_version
        run.worker_id = self.worker.worker_id
        run.lease_expires_at = claimed.lease_expires_at
        run.attempt = claimed.attempt or 1
        run.transition(STATUS_RUNNING)
        self.worker.supervise(
            run,
            adapter,
            RunJob(
                run_id=run_id,
                user_id=user_id,
                messages=messages,
                agent_config=agent_config,
                use_memory_context=use_memory_context,
                db_info=db_info,
            ),
        )
        return run, adapter, self._bus_stream(run, adapter)

    async def _begin_run(
        self,
        *,
        agent_config: Dict[str, Any],
        user_id: int,
        session_id: Optional[int],
        idempotency_key: Optional[str],
        request_hash: Optional[str],
        input_payload: Dict[str, Any],
        snapshot: Dict[str, Any],
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Tuple[AgentRunRow, bool, Optional[int]]:
        """业务事务（P1-01/P1-02）：幂等预查 + Session 创建 + 用户消息追加
        + Run 创建 + Session 执行权 Claim，单事务原子提交。

        - 幂等命中（同 Key 同摘要）：返回原 Run，不触碰 Session/消息；
        - 同 Key 不同摘要：``IdempotencyConflict``（API 映射 409）；
        - 并发同 Key：后到者在 Run 唯一约束上收敛为复用，同一事务回滚，
          不产生孤立 Session、不重复追加用户消息；
        - Session 执行权 CAS 失败：整事务回滚并抛 ``SessionBusy``（明确冲突）。
        """
        async with AsyncSessionLocal() as db:
            repo = RunRepository(db)
            sess_repo = SessionRepository(db)

            # 1) 幂等命中：同摘要复用原 Run，绝不重复执行/追加用户消息
            if idempotency_key:
                existing = await repo.get_by_idempotency_key(user_id, idempotency_key)
                if existing is not None:
                    checked = RunRepository._check_reuse(existing, request_hash)
                    return checked, False, checked.session_id

            # 2) Session 定位 / 创建（与 Run 创建同事务）
            resolved_session_id = session_id
            if resolved_session_id is None and user_message is not None:
                new_sess = AgentSession(
                    agent_id=agent_config["id"],
                    user_id=user_id,
                    messages=[],
                )
                db.add(new_sess)
                await db.flush()
                resolved_session_id = new_sess.id

            # 3) 用户消息追加（稳定 message_id + version 乐观锁合并；与 Run 原子）
            if user_message is not None and resolved_session_id is not None:
                content = str(user_message.get("content") or "")
                await sess_repo.append_messages(
                    resolved_session_id,
                    [{
                        "message_id": uuid.uuid4().hex,
                        "role": user_message.get("role") or "user",
                        "content": content,
                        "created_at": datetime.utcnow().isoformat(),
                    }],
                    title=content[:50] if content else None,
                )

            # 4) Run 创建（幂等唯一约束兜底并发；thread_id 随解析后的 Session 生成）
            thread_id = make_thread_id(
                agent_config["id"], resolved_session_id, uuid.uuid4().hex
            )
            row, created = await repo.create_run(
                run_id=uuid.uuid4().hex,
                agent_id=agent_config["id"],
                session_id=resolved_session_id,
                user_id=user_id,
                thread_id=thread_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                input_payload=input_payload,
                agent_config_snapshot=snapshot,
            )
            if not created:
                # 并发幂等重试收敛（create_run 冲突路径已 rollback 本事务）：
                # 返回既有 Run，不重复执行、Session 创建/消息追加随回滚撤销
                return row, False, row.session_id

            # 5) Session 执行权原子 Claim（P1-02 A：单 Session 单活跃 Run）
            if resolved_session_id is not None:
                ok = await sess_repo.claim_execution(
                    resolved_session_id, row.id, self.worker.lease_seconds
                )
                if not ok:
                    raise SessionBusy(
                        f"session {resolved_session_id} 有正在执行的 Run，请稍后重试"
                    )

            await db.commit()
            return row, True, resolved_session_id

    async def _bus_stream(
        self, run: RunHandle, adapter: LegacyEventAdapter
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """本进程执行：直连事件总线的旧契约事件流。

        消费者必然终止（任务书 §12.2：SSE / 本地流禁止悬挂等待）：
        除流边界事件（终态 / waiting_approval / run_suspended）外，当驱动任务
        已结束且队列排空仍无终止事件（如持久化失败、异常退出）时，按
        run.status 合成终止事件并结束本次流。
        """
        while True:
            ev = await run.bus.get(timeout=0.5)
            if ev is None:
                task = run.task
                if task is not None and task.done():
                    synthetic = self._synthesize_terminal(run)
                    for legacy in adapter.adapt(synthetic):
                        yield legacy
                    return
                continue
            for legacy in adapter.adapt(ev):
                yield legacy
            if ev.type in TERMINAL_EVENTS:
                return

    @staticmethod
    def _synthesize_terminal(run: RunHandle) -> RuntimeEvent:
        """驱动任务结束但无终止事件时的兜底合成事件（按 run.status 还原终态）。"""
        status = run.status or STATUS_RUNNING
        if status == STATUS_COMPLETED:
            return RuntimeEvent(type=RUN_COMPLETED, run_id=run.run_id,
                                data={"final_text": run.final_text, "summary": run.summary})
        if status == STATUS_FAILED:
            return RuntimeEvent(type=RUN_FAILED, run_id=run.run_id,
                                data={"error": run.error or "执行链路异常结束"})
        if status == STATUS_LIMIT_EXCEEDED:
            return RuntimeEvent(type=RUN_FAILED, run_id=run.run_id,
                                data={"error": run.error or "执行预算超限",
                                      "status": STATUS_LIMIT_EXCEEDED})
        if status == STATUS_CANCELLED:
            return RuntimeEvent(type=RUN_CANCELLED, run_id=run.run_id)
        if status in (STATUS_WAITING_APPROVAL, STATUS_NEEDS_RECONCILIATION):
            return RuntimeEvent(type=RUN_WAITING_APPROVAL, run_id=run.run_id,
                                data={"approval_required": True, "status": status})
        return RuntimeEvent(type=RUN_SUSPENDED, run_id=run.run_id,
                            data={"reason": "drive_ended_without_terminal_event"})

    async def _replay_db_stream(
        self, run_id: str, run: RunHandle, adapter: LegacyEventAdapter
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """幂等命中 / 跨进程执行时的事件流：统一从事件仓库消费（FR-07）。"""
        async for legacy in self.replay_events(run_id):
            yield legacy

    async def replay_events(
        self,
        run_id: str,
        after_seq: Optional[int] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """按持久化事件日志消费 Run 事件流（SSE 断线回放 + 跨进程订阅，FR-07）。

        - 事实来源为 ``agent_run_events``；内存 EventBus 仅是本地通知优化，
          因此 API 实例与执行 Worker 不同进程也能消费到完整事件流；
        - 旧契约事件 seq 从 0 连续递增：``after_seq`` 之前的事件仅重建
          适配器状态不重复下发，之后逐条下发直至终态（不丢失、不重复、
          不乱序）；``after_seq=None`` 表示从头消费（含 seq=0）；
        - 无事件日志的旧运行按状态合成旧契约终态，保持行为兼容。
        """
        adapter = LegacyEventAdapter(run_id)
        db_seq = 0
        poll = max(0.05, float(getattr(settings, "AGENT_RUNTIME_POLL_INTERVAL", 1.0)))

        def _visible(ev_dict: Dict[str, Any]) -> bool:
            return after_seq is None or int(ev_dict.get("seq") or 0) > after_seq

        while True:
            async with AsyncSessionLocal() as db:
                rows = await EventRepository(db).list_after(run_id, db_seq)
            if rows:
                for row in rows:
                    db_seq = row.sequence
                    ev = RuntimeEvent(
                        type=row.event_type,
                        run_id=run_id,
                        data=row.payload or {},
                        sequence=row.sequence,
                        thread_id=row.thread_id or "",
                    )
                    for old in adapter.adapt(ev):
                        if _visible(old):
                            yield old
                if adapter.finished:
                    return
                continue

            async with AsyncSessionLocal() as db:
                run_row = await RunRepository(db).get_run(run_id)
            if run_row is None:
                if db_seq == 0:
                    yield {"type": "error", "content": "运行不存在"}
                    yield {"type": "done", "status": "failed"}
                return
            if run_row.status in TERMINAL_STATUSES or run_row.status == STATUS_WAITING_APPROVAL:
                if adapter.finished:
                    return
                # 事件日志缺失/已清理的旧运行：按状态合成旧契约终态
                for old in adapter.adapt(_synthetic_terminal_event(run_row)):
                    if _visible(old):
                        yield old
                return
            await asyncio.sleep(poll)

    async def _drive(
        self,
        run: RunHandle,
        adapter: LegacyEventAdapter,
        *,
        messages: Optional[List[Dict[str, Any]]],
        resume: Optional[Dict[str, Any]],
        agent_config: Optional[Dict[str, Any]] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
    ) -> None:
        bus = run.bus
        ctx: Optional[RuntimeContext] = None
        log_fields: Dict[str, Any] = {
            "request_id": run.run_id,
            "trace_id": run.run_id,
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "agent_id": run.agent_id,
            "tenant_id": None,
            "user_id": user_id or run.user_id,
            "session_id": run.session_id,
            "model_id": None,
        }
        try:
            if agent_config is None:
                agent_config = await self._load_agent_config(run.agent_id)
            agent_config = dict(agent_config)
            mem_ctx = use_memory_context or agent_config.pop("_memory_context", "")
            extra_db_info = db_info if db_info is not None else agent_config.pop("_db_info", None)

            provider, model_config = await self._load_model(agent_config.get("model_config_id"))
            log_fields["model_id"] = model_config.get("name")

            scope = RunScope(user_id=user_id or run.user_id)
            ctx = RuntimeContext(
                scope=scope,
                run_id=run.run_id,
                thread_id=run.thread_id,
                agent_id=run.agent_id,
                session_id=run.session_id,
                trace_id=run.run_id,
                model_id=log_fields["model_id"],
                agent_config=agent_config,
                memory_context=mem_ctx,
                budget=ExecutionBudget(
                    max_steps=getattr(settings, "AGENT_MAX_STEPS", 20),
                    max_tool_calls=getattr(settings, "AGENT_MAX_TOOL_CALLS", 30),
                    max_model_calls=getattr(settings, "AGENT_MAX_MODEL_CALLS", 30),
                    run_timeout_seconds=getattr(settings, "AGENT_RUN_TIMEOUT", 300.0),
                ),
                enable_hitl=getattr(settings, "AGENT_ENABLE_HITL", True),
                sandbox_mode=getattr(settings, "AGENT_SANDBOX_MODE", "restricted"),
                allow_network=getattr(settings, "AGENT_ALLOW_NETWORK", True),
                worker_id=run.worker_id,
                lease_version=run.lease_version or 0,
            )

            tool_specs = self._resolve_tool_specs(agent_config)
            policy = ToolPolicy.from_settings({s["name"] for s in tool_specs}, enable_hitl=ctx.enable_hitl)

            # 工具 db 会话：独立 session，避免污染请求生命周期
            async with AsyncSessionLocal() as tool_db:
                ctx.db_info = dict(extra_db_info or {})
                ctx.db_info["session"] = tool_db
                if extra_db_info and not ctx.db_info.get("hint"):
                    ctx.db_info["hint"] = self._db_hint(extra_db_info)

                checkpointer = await CheckpointerFactory.get().get_checkpointer()
                store = await CheckpointerFactory.get().get_store()
                agent = build_agent(
                    ctx, policy, bus, provider, model_config, tool_specs,
                    checkpointer=checkpointer, store=store,
                )

                await bus.publish(RuntimeEvent(type=RUN_STARTED, run_id=run.run_id))
                telemetry.record_run(log_fields, status="running")

                skill_files = self._skill_files()
                if resume is not None:
                    payload: Any = Command(resume=resume)
                elif messages is None:
                    # 恢复续跑：Checkpoint 已含全部输入，从断点继续（不重复写入消息）
                    payload = None
                else:
                    payload = {
                        "messages": messages_to_langchain(
                            messages or [], namespace=run.thread_id
                        ),
                        **skill_files,
                    }

                config = make_run_config(run.thread_id, run.run_id, scope.user_id)
                config["recursion_limit"] = ctx.budget.max_steps * 3 + 10
                # FR-10：模型调用按一次请求计数（Runnable 生命周期回调，不按 Chunk）
                config["callbacks"] = [ModelUsageCallback(ctx)]
                run_config = config

                # FR-09：挂起/重启恢复后从事件日志重建已产生的最终文本片段，
                # 保证跨 _drive 调用（审批恢复 / 崩溃恢复）最终文本完整
                if not run.final_text_parts:
                    run.final_text_parts = await self._load_prior_final_text(run.run_id)

                try:
                    async with asyncio.timeout(ctx.budget.run_timeout_seconds):
                        await self._consume_stream(agent, payload, run_config, ctx, run, bus)
                except asyncio.TimeoutError:
                    raise _RunLimitExceeded(f"运行超时（>{ctx.budget.run_timeout_seconds}s）")

            # 结束分支
            if run.status == STATUS_RUNNING:
                # FR-09：run.final_text 唯一权威收集在执行侧（_consume_stream 累积
                # 的 run.final_text_parts），事件适配器只做协议转换；
                # 先持久化最终结果，再写回 Session、最后才发布 RUN_COMPLETED
                run.final_text = "".join(run.final_text_parts)
                run.summary = {
                    "tool_calls": ctx.tool_calls,
                    "model_calls": ctx.model_calls,
                    "model_node_runs": ctx.model_node_runs,
                    "tokens_generated": ctx.tokens_generated,
                    "steps": ctx.steps,
                    "elapsed_seconds": round(ctx.elapsed(), 2),
                }
                run.transition(STATUS_COMPLETED)
                await self._commit(
                    run,
                    status=STATUS_COMPLETED,
                    final_text=run.final_text,
                    summary=run.summary,
                    finished=True,
                )
                try:
                    await self._write_back_session(run)  # FR-08.2：Runtime 统一写回
                except Exception:
                    # P1-02 C：写回失败不改变 Run 终态、不影响事件流（Outbox 重试）
                    logger.exception("run %s 完成态会话写回失败", run.run_id)
                await bus.publish(RuntimeEvent(
                    type=RUN_COMPLETED,
                    run_id=run.run_id,
                    data={"final_text": run.final_text, "summary": run.summary},
                ))

        except PersistenceError:
            # 关键状态写入失败：立即停止推进，绝不带病执行副作用（R-12 / 任务书 §0.7）
            logger.critical("run %s 关键状态持久化失败，停止推进", run.run_id, exc_info=True)
            # 尽力通知本地消费者终止（持久化可能已不可用，仅入内存队列）；
            # 失败也无碍：消费端还有"驱动结束即合成终止事件"的兜底
            try:
                await bus.publish(RuntimeEvent(type=RUN_FAILED, run_id=run.run_id,
                                               data={"error": "关键状态持久化失败，执行已停止"}))
            except Exception:
                pass
            raise
        except asyncio.CancelledError:
            if run.suspend_requested:
                # 挂起（优雅关闭 / 租约丢失）：不改写状态，保留 running 供其他 Worker 恢复
                logger.info("run %s 挂起执行（不写入 cancelled）", run.run_id)
                # 挂起必须通知本地消费者（任务书 §12.2：SSE 不得悬挂）：
                # run_suspended 是流边界事件，收到即结束本次事件流
                try:
                    await bus.publish(RuntimeEvent(
                        type=RUN_SUSPENDED,
                        run_id=run.run_id,
                        data={"reason": run.suspend_reason or "shutdown"},
                    ))
                except Exception:
                    logger.exception("run %s 挂起事件发布失败", run.run_id)
                raise
            if not run.is_terminal:
                try:
                    run.transition(STATUS_CANCELLED)
                except Exception:
                    pass
            committed = True
            try:
                await self._commit(run, status=STATUS_CANCELLED, finished=True)
            except PersistenceError:
                logger.critical("run %s 取消状态持久化失败", run.run_id, exc_info=True)
            except (RunLeaseLost, IllegalTransition) as e:
                # 提交被拒：不得谎报 cancelled —— 不写回、不发布取消事件
                logger.warning("run %s 取消态提交被拒：%s", run.run_id, e)
                committed = False
            if committed:
                try:
                    await self._write_back_session(run)  # FR-08.2：终态统一写回
                except Exception:
                    logger.exception("run %s 取消态会话写回失败", run.run_id)
                await bus.publish(RuntimeEvent(type=RUN_CANCELLED, run_id=run.run_id))
            raise
        except _RunLimitExceeded as e:
            await self._fail(run, str(e), status=STATUS_LIMIT_EXCEEDED, bus=bus)
        except (ToolCallLimitExceededError, ModelCallLimitExceededError, GraphRecursionError) as e:
            # 执行预算超限（工具/模型次数、步数）→ 明确 limit_exceeded 状态
            await self._fail(run, f"执行预算超限: {e}", status=STATUS_LIMIT_EXCEEDED, bus=bus)
        except BudgetExceededError as e:
            # FR-10.7：预算耗尽 → limit_exceeded（不得误报 failed）
            await self._fail(run, str(e), status=STATUS_LIMIT_EXCEEDED, bus=bus)
        except ModelCapabilityError as e:
            await self._fail(run, f"模型能力不满足: {e}", status=STATUS_FAILED, bus=bus)
        except ToolPolicyError as e:
            await self._fail(run, f"安全策略拒绝: {e}", status=STATUS_FAILED, bus=bus)
        except RunLeaseLost as e:
            # P0-02 E / LF-04：fencing 失败 —— 立即中止推进：
            # 不写 Session、不发布 RUN_COMPLETED / RUN_FAILED、不产生新副作用，
            # 保留数据库中当前有效 Worker 的状态；本地流以 run_suspended 边界终止
            logger.warning("run %s 执行权丢失（fencing 拒绝），中止推进：%s", run.run_id, e)
            try:
                await bus.publish(RuntimeEvent(
                    type=RUN_SUSPENDED, run_id=run.run_id,
                    data={"reason": "lease_lost"},
                ))
            except Exception:
                logger.exception("run %s 执行权丢失事件发布失败", run.run_id)
            raise
        except Exception as e:
            logger.exception("run %s 执行失败", run.run_id)
            await self._fail(run, str(e), status=STATUS_FAILED, bus=bus)
        finally:
            telemetry.record_run(
                log_fields,
                status=run.status,
                duration_ms=round((ctx.elapsed() if ctx else 0.0) * 1000, 1) or None,
                error=run.error,
                attributes=run.summary or {},
            )

    async def _consume_stream(self, agent, payload, run_config, ctx, run, bus) -> None:
        """消费 astream：模型增量 / 计划 / 中断 -> 事件总线。

        FR-09：run.final_text 的唯一权威收集在本函数（执行侧）——可见模型
        文本在发布 MODEL_DELTA 前先累积到 run.final_text_parts，事件适配器
        只做协议转换，不承担最终结果收集。
        FR-10：模型调用次数由 ModelUsageCallback 按请求计数（此处不按 Chunk
        递增）；steps/model_node_runs 递增位置 = updates 中主图 model 节点
        完成（一次 = 一个 agent 轮次）。
        """
        interrupted = False
        async for mode, chunk in agent.astream(
            payload, config=run_config, stream_mode=["updates", "messages"]
        ):
            if mode == "messages":
                msg, meta = chunk
                if isinstance(msg, (AIMessage, AIMessageChunk)) and meta.get("langgraph_node") == "model":
                    # 模型增量（含非流式整段消息；仅主模型节点可见文本）
                    # FR-09.5：结构化/多模态内容按明确规则提取文本，不静默丢弃
                    text = _extract_text(msg.content)
                    if text:
                        # FR-09：权威收集（执行侧）——先累积再发布，消费端时序无关
                        run.final_text_parts.append(text)
                        await bus.publish(RuntimeEvent(
                            type=MODEL_DELTA, run_id=run.run_id, data={"text": text}
                        ))
            elif mode == "updates":
                if not isinstance(chunk, dict):
                    continue
                if "model" in chunk:
                    ctx.model_node_runs += 1
                    ctx.steps += 1
                if "__interrupt__" in chunk:
                    interrupted = True
                    await self._handle_interrupt(chunk["__interrupt__"], run, bus)
                    continue
                for node_update in chunk.values():
                    if isinstance(node_update, dict) and "todos" in node_update:
                        todos = node_update.get("todos") or []
                        await bus.publish(RuntimeEvent(
                            type=PLAN_UPDATED, run_id=run.run_id,
                            data={"todos": [dict(t) if not isinstance(t, dict) else t for t in todos]},
                        ))
        if interrupted:
            run.transition(STATUS_WAITING_APPROVAL)
            # 暂停态必须先持久化成功，再对外宣告（失败向上抛出）
            await self._commit(run, status=STATUS_WAITING_APPROVAL)
            await bus.publish(RuntimeEvent(
                type=RUN_WAITING_APPROVAL, run_id=run.run_id,
                data={"actions": redact_sensitive(run.pending_actions)},
            ))

    async def _handle_interrupt(self, interrupts, run, bus) -> None:
        actions: List[Dict[str, Any]] = []
        items = interrupts
        if isinstance(items, tuple):
            items = list(items)
        for it in items:
            value = _field(it, "value", it)
            interrupt_id = str(_field(it, "id", "") or "")
            if isinstance(value, dict) and "action_requests" in value:
                for ridx, req in enumerate(value["action_requests"]):
                    actions.append(_action_from_req(req, interrupt_id, ridx))
        run.pending_actions = actions
        await self._record_approval_requests(run.run_id, actions)
        await bus.publish(RuntimeEvent(
            type=APPROVAL_REQUIRED, run_id=run.run_id, data={"actions": actions}
        ))

    async def _fail(self, run: RunHandle, error: str, *, status: str, bus: EventBus) -> None:
        """失败路径（P0-02 F）：不得覆盖已提交的终态。

        - Run 已是终态（如 completed 后 Session 写回异常）：直接返回，
          不写库、不发布 RUN_FAILED（完成态不得降级为 failed）；
        - 非法状态迁移：不吞掉后继续写库；
        - 提交被拒（RunLeaseLost / IllegalTransition）：跳过写库与事件发布
          （数据库 CAS 是最终状态权威）。
        """
        if run.is_terminal:
            logger.warning(
                "run %s 已处于终态 %s，拒绝降级为 %s（不写库、不发布失败事件）",
                run.run_id, run.status, status,
            )
            return
        try:
            run.transition(status)
        except InvalidTransition as e:
            logger.warning(
                "run %s 非法状态迁移 %s -> %s，拒绝写库：%s",
                run.run_id, run.status, status, e,
            )
            return
        run.error = error
        try:
            await self._commit(run, status=status, error=error, finished=True)
        except (RunLeaseLost, IllegalTransition):
            # 提交被拒：不写 Session、不发布 RUN_FAILED（不覆盖有效状态）
            return
        try:
            await self._write_back_session(run)  # FR-08.2：终态统一写回
        except Exception:
            logger.exception("run %s 失败态会话写回失败", run.run_id)
        await bus.publish(RuntimeEvent(
            type=RUN_FAILED, run_id=run.run_id,
            data={"error": error, "status": status},
        ))

    async def _load_prior_final_text(self, run_id: str) -> List[str]:
        """FR-09：从持久化事件日志重建本 Run 已产生的最终文本片段（MODEL_DELTA），
        供挂起/重启恢复后的 _drive 调用继续累积，保证最终文本跨恢复完整。"""
        async with AsyncSessionLocal() as db:
            rows = await EventRepository(db).list_after(run_id, 0)
        return [
            r.payload.get("text", "")
            for r in rows
            if r.event_type == MODEL_DELTA and r.payload and r.payload.get("text")
        ]

    async def _write_back_session(self, run: RunHandle) -> None:
        """FR-08.2 + P1-02 C：Assistant 最终结果经 Outbox 写回 Session。

        Run 终态与写回解耦：终态提交时已同事务入队 ``SessionResultOutbox``，
        本方法执行实际写回——按 ``source_run_id`` 去重 + ``version`` 乐观锁合并，
        写回与 ``mark_delivered`` 同事务提交（重试不会重复追加 Assistant 消息）。
        写回失败仅增加 ``attempts`` 并保留 pending（Outbox Worker 重试，服务
        重启后可恢复），绝不改变 Run 终态。写回失败向上抛出（Completion 路径
        调用方先已持久化 Run 结果，失败态路径调用方仅记日志）。
        """
        await self._deliver_outbox(run.run_id)

    async def _deliver_outbox(self, run_id: str) -> None:
        """按 Outbox 记录执行一次 Session 写回（幂等、可重试）。"""
        async with AsyncSessionLocal() as db:
            outbox = await SessionOutboxRepository(db).get_by_run(run_id)
            row = await RunRepository(db).get_run(run_id)

        if outbox is not None and outbox.status == OUTBOX_DELIVERED:
            return  # 已写回：幂等跳过
        session_id = row.session_id if row is not None else (
            outbox.session_id if outbox is not None else None
        )
        if session_id is None:
            if outbox is not None:
                async with AsyncSessionLocal() as db:
                    await SessionOutboxRepository(db).mark_delivered(run_id)
                    await db.commit()
            return

        steps: List[Dict[str, Any]] = []
        async for legacy in self.replay_events(run_id):
            if legacy.get("type") != "done":
                steps.append(legacy)

        try:
            async with AsyncSessionLocal() as db:
                await SessionRepository(db).append_messages(
                    session_id,
                    [{
                        "message_id": uuid.uuid4().hex,
                        "role": "assistant",
                        "content": (row.final_text or "") if row is not None else "",
                        "steps": steps,
                        "source_run_id": run_id,
                        "created_at": datetime.utcnow().isoformat(),
                    }],
                    source_run_id=run_id,
                )
                outbox_repo = SessionOutboxRepository(db)
                if await outbox_repo.get_by_run(run_id) is None:
                    await outbox_repo.enqueue(run_id, session_id)
                await outbox_repo.mark_delivered(run_id)
                await db.commit()
        except LookupError:
            # 会话已删除：跳过写回并标记完成，避免无限重试
            logger.warning("run %s 会话 %s 不存在，跳过写回", run_id, session_id)
            await self._settle_outbox(run_id, session_id, error=None)
        except Exception as e:
            # 写回失败：保留 pending 供重试（服务重启后可恢复），向上抛出
            await self._settle_outbox(run_id, session_id, error=str(e))
            raise

    async def _settle_outbox(
        self, run_id: str, session_id: Optional[int], *, error: Optional[str]
    ) -> None:
        async with AsyncSessionLocal() as db:
            outbox_repo = SessionOutboxRepository(db)
            if await outbox_repo.get_by_run(run_id) is None:
                await outbox_repo.enqueue(run_id, session_id)
            if error is None:
                await outbox_repo.mark_delivered(run_id)
            else:
                await outbox_repo.record_failure(run_id, error)
            await db.commit()

    async def deliver_pending_outbox(self, limit: int = 20) -> int:
        """Outbox Worker：恢复扫描 pending 写回任务。

        服务重启后未完成的写回可继续恢复；已 delivered 的按 source_run_id
        幂等跳过，重试不会重复追加 Assistant 消息。
        """
        async with AsyncSessionLocal() as db:
            rows = await SessionOutboxRepository(db).list_pending(limit)
            run_ids = [r.run_id for r in rows]
        delivered = 0
        for rid in run_ids:
            try:
                await self._deliver_outbox(rid)
                delivered += 1
            except Exception as e:
                logger.warning(
                    "outbox 写回 run %s 失败（保留 pending 供重试）：%s", rid, e
                )
        return delivered

    # ── 内部：装载 ────────────────────────────────────────────────────────

    async def _load_agent_config(self, agent_id: int) -> Dict[str, Any]:
        from sqlalchemy import select
        from app.models.agent import Agent

        async with AsyncSessionLocal() as db:
            agent = (await db.execute(
                select(Agent).where(Agent.id == agent_id, Agent.is_active == True)  # noqa: E712
            )).scalar_one_or_none()
            if agent is None:
                raise RuntimeError(f"Agent {agent_id} 不存在或已禁用")
            return {
                "id": agent.id,
                "name": agent.name,
                "model_config_id": agent.model_config_id,
                "system_prompt": agent.system_prompt,
                "max_iterations": agent.max_iterations,
                "temperature": agent.temperature,
                "tools": agent.tools or [],
                "knowledge_base_id": agent.knowledge_base_id,
                "database_connection_id": agent.database_connection_id,
            }

    async def _load_model(self, model_config_id: Optional[int]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        from sqlalchemy import select
        from app.models.model_provider import ModelConfig, ModelProvider

        if not model_config_id:
            raise ModelCapabilityError("Agent 未配置模型（model_config_id 为空）")
        async with AsyncSessionLocal() as db:
            mc = (await db.execute(select(ModelConfig).where(ModelConfig.id == model_config_id))).scalar_one_or_none()
            if mc is None:
                raise ModelCapabilityError(f"模型配置 {model_config_id} 不存在")
            provider = (await db.execute(select(ModelProvider).where(ModelProvider.id == mc.provider_id))).scalar_one_or_none()
            if provider is None:
                raise ModelCapabilityError(f"模型供应商不存在（model_config_id={model_config_id}）")
            return (
                {"name": provider.name, "base_url": provider.base_url, "api_key": provider.api_key,
                 "extra_config": provider.extra_config or {}},
                {"name": mc.name, "max_tokens": mc.max_tokens, "supports_streaming": mc.supports_streaming,
                 "supports_function_call": mc.supports_function_call, "extra_params": mc.extra_params or {}},
            )

    def _resolve_tool_specs(self, agent_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        from app.services.tools import get_tool_by_name

        specs = []
        for item in (agent_config.get("tools") or []):
            name = item["name"] if isinstance(item, dict) else item
            enabled = item.get("enabled", True) if isinstance(item, dict) else True
            if not enabled:
                continue
            spec = get_tool_by_name(name)
            if spec:
                specs.append(spec)
        return specs

    def _skill_files(self) -> Dict[str, Any]:
        from .skills import SkillRegistry

        registry = SkillRegistry.from_settings()
        files = registry.files_payload()
        return {"files": files} if files else {}

    def _db_hint(self, db_info: Dict[str, Any]) -> str:
        tables = ", ".join(db_info.get("tables") or []) or "(未预加载)"
        return (
            f"## 数据库连接\n已连接数据库 `{db_info.get('database')}`（主机 {db_info.get('host')}）。\n"
            f"db_query/db_count/db_aggregate 工具将作用于该数据库（只读）。可用表：{tables}"
        )

    # ── 内部：持久化（关键写入失败一律向上抛出） ──────────────────────────

    async def _commit(
        self,
        run: RunHandle,
        *,
        status: str,
        error: Optional[str] = None,
        final_text: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        recovery_error: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        """提交 Run 状态（P0-02：全条件 Fencing，失败显式抛出）。

        - ``RunLeaseLost``：租约易主/过期 —— 调用方必须立即停止推进
          （不写 Session、不发布终态事件、不产生新副作用）；
        - ``IllegalTransition``：终态覆盖/非法迁移 —— 不得继续写库；
        - ``PersistenceError``：数据库异常（R-12）。
        不再返回易被忽略的布尔值：所有调用点必须显式处理异常。
        """
        lease = run.execution_lease()
        if lease is None:
            raise RunLeaseLost(
                f"run {run.run_id} 无有效执行权（worker/lease_version 缺失），状态 {status} 提交被拒"
            )
        try:
            async with AsyncSessionLocal() as db:
                await RunRepository(db).update_status(
                    run.run_id,
                    status=status,
                    lease=lease,
                    error=error,
                    final_text=final_text,
                    summary=summary,
                    recovery_error=recovery_error,
                    finished=finished,
                )
                if finished and run.session_id:
                    # Run 终态与 Session 写回解耦（P1-02 C）：同一事务提交
                    # Run 终态 + SessionResultOutbox（写回由 Outbox Worker 完成，
                    # 写回失败/重试不改变 Run 终态）；同事务释放 Session 执行权。
                    await SessionOutboxRepository(db).enqueue(
                        run.run_id, run.session_id
                    )
                    await SessionRepository(db).release_execution(
                        run.session_id, run.run_id
                    )
                await db.commit()
        except (RunLeaseLost, IllegalTransition) as e:
            logger.warning(
                "run %s 状态 %s 提交被拒（fencing/状态机）：%s",
                run.run_id, status, e,
            )
            raise
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(f"run {run.run_id} 状态持久化失败: {e}") from e

    async def _recovery_check(
        self, row: Any
    ) -> Tuple[str, str, List[Dict[str, Any]], Dict[str, Any]]:
        """恢复决策（attempt > 1 的重新领取必须先判断可否安全继续，P0-04 §6.2 A）。

        返回 (mode, reason, pending_actions, metadata)：
        - fresh：无 Checkpoint 且无结果未知的副作用，用输入快照执行；
        - continue：有 Checkpoint 且无未决中断，从断点续跑（重放复用原
          tool_call_id / 幂等键）；
        - waiting_approval：Checkpoint 停在 HITL 中断，回到待审批；
        - needs_reconciliation：结果未知的副作用 / Checkpoint 损坏，禁止自动重放。

        决策唯一来源是 ``classification.recovery_decision``（§6.2 A 决策表）：
        - 无 Checkpoint + 结果未知的 external_idempotent/non_idempotent 副作用
          → 阻断（禁止重新推理生成新调用 ID）；
        - Checkpoint 不可读/损坏 → 安全阻断 + recovery_error，绝不 fresh restart。

        所有恢复决策写入日志与 ``agent_runs.recovery_metadata``（阻断原因同时
        写入 ``recovery_error``），满足 §6.3 可审计要求。
        """
        if (getattr(row, "attempt", 0) or 0) <= 1:
            return RECOVERY_FRESH, "", [], {}

        async with AsyncSessionLocal() as db:
            entries = await ToolExecutionRepository(db).list_unresolved(row.id)

        state = await self.get_thread_state(row.thread_id)
        checkpoint_error: Optional[str] = None
        if state is None:
            checkpoint_state = CHECKPOINT_MISSING
        elif state.get("error"):
            checkpoint_state = CHECKPOINT_UNREADABLE
            checkpoint_error = str(state["error"])
        else:
            checkpoint_state = CHECKPOINT_OK

        decision = recovery_decision(checkpoint_state, entries)
        mode, reason = decision.mode, decision.reason
        actions: List[Dict[str, Any]] = []
        if mode == RECOVERY_CONTINUE and state.get("has_interrupt"):
            mode = RECOVERY_WAITING_APPROVAL
            actions = state.get("pending_actions") or []
            reason = "Checkpoint 停在 HITL 中断，回到待审批"

        metadata: Dict[str, Any] = {
            "mode": mode,
            "reason": reason,
            "checkpoint_state": checkpoint_state,
            "checkpoint_error": checkpoint_error,
            "pending": decision.pending,
            "decided_at": datetime.utcnow().isoformat() + "Z",
        }
        logger.info(
            "run %s 恢复决策: mode=%s checkpoint=%s pending=%d reason=%s",
            row.id, mode, checkpoint_state, len(decision.pending), reason or "-",
        )

        lease = RunExecutionLease(
            run_id=row.id,
            worker_id=row.worker_id,
            lease_version=row.lease_version,
            lease_expires_at=row.lease_expires_at,
        )
        async with AsyncSessionLocal() as db:
            await RunRepository(db).record_recovery(
                row.id,
                lease,
                metadata=metadata,
                recovery_error=reason if mode == RECOVERY_NEEDS_RECONCILIATION else None,
            )
            await db.commit()
        return mode, reason, actions, metadata

    async def _record_approval_requests(self, run_id: str, actions: List[Dict[str, Any]]) -> None:
        """审批请求先持久化（FR-06.2）：action_id 幂等，重启重放不产生重复记录。"""
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        try:
            async with AsyncSessionLocal() as db:
                for a in actions:
                    action_id = str(a.get("action_id") or hashlib.sha256(
                        (str(a.get("tool")) + json.dumps(
                            a.get("args") or {}, ensure_ascii=False, sort_keys=True, default=str
                        )).encode("utf-8")
                    ).hexdigest()[:40])
                    existing = (await db.execute(
                        select(AgentApproval).where(
                            AgentApproval.run_id == run_id,
                            AgentApproval.action_id == action_id,
                        )
                    )).scalar_one_or_none()
                    if existing is not None:
                        continue  # 幂等：重放/重启不重复登记
                    db.add(AgentApproval(
                        run_id=run_id,
                        tool_name=str(a.get("tool") or "unknown"),
                        action_summary=json.dumps(a.get("args") or {}, ensure_ascii=False)[:2000],
                        action_id=action_id,
                        tool_call_id=a.get("tool_call_id"),
                        args_hash=a.get("args_hash"),
                        action_payload=a.get("args") or None,
                        decision_status="pending",
                    ))
                await db.commit()
        except Exception as e:
            raise PersistenceError(f"run {run_id} 审批记录写入失败: {e}") from e

    @staticmethod
    def _match_approval_row(rows: List[Any], dec: Dict[str, Any], idx: int) -> Optional[Any]:
        """决策与审批动作匹配：带 action_id 精确匹配，否则按登记顺序对齐。"""
        action_id = dec.get("action_id")
        if action_id is not None:
            for r in rows:
                if r.action_id == action_id:
                    return r
            return None
        return rows[idx] if idx < len(rows) else None

    async def _check_decisions(self, run_id: str, decisions: List[Dict[str, Any]]) -> str:
        """审批决策校验（不落库）。返回 'ok'（可受理）| 'idempotent'（重复一致提交）。

        - 未知决策类型 / 未知审批动作 -> ApprovalInvalid
        - 同一动作改判 -> ApprovalConflict（F-11：冲突必须显式拒绝，不得静默覆盖）
        """
        valid_types = {"approve", "reject"}
        if not decisions:
            raise ApprovalInvalid("审批决策为空")
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AgentApproval).where(AgentApproval.run_id == run_id).order_by(AgentApproval.id)
            )).scalars().all()
        verdicts: List[str] = []
        for i, dec in enumerate(decisions):
            dtype = str(dec.get("type") or "").strip()
            if dtype not in valid_types:
                raise ApprovalInvalid(f"未知决策类型: {dtype!r}（仅支持 approve / reject）")
            row = self._match_approval_row(rows, dec, i)
            if row is None:
                raise ApprovalInvalid(f"未知审批动作: {dec.get('action_id') or f'#{i + 1}'}")
            if row.decision is not None:
                if row.decision == dtype:
                    verdicts.append("idempotent")
                else:
                    raise ApprovalConflict(
                        f"审批动作 {row.action_id or row.id} 已决策为 {row.decision}，"
                        f"与新决策 {dtype} 冲突"
                    )
            else:
                verdicts.append("record")
        return "idempotent" if verdicts and all(v == "idempotent" for v in verdicts) else "ok"

    async def check_decisions(
        self,
        run_id: str,
        decisions: List[Dict[str, Any]],
        deciding_user_id: int,
    ) -> str:
        """审批前置校验（不落库，API 层映射 HTTP 403/400/409）。"""
        run = self.registry.get(run_id)
        owner: Optional[int] = run.user_id if run is not None else None
        if run is None:
            from sqlalchemy import select
            from app.models.agent import AgentRun

            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )).scalar_one_or_none()
            if row is None:
                raise ApprovalInvalid("运行不存在或不在待审批状态")
            owner = row.user_id
        if owner not in (None, deciding_user_id):
            raise ApprovalForbidden("无权审批该运行")
        return await self._check_decisions(run_id, decisions)

    async def _record_approval_decision(self, run_id: str, decisions: List[Dict[str, Any]], user_id: int) -> None:
        """审批决策先持久化（FR-04.6）：冲突/非法决策拒绝；重复一致决策幂等。

        关键写入失败必须向上抛出：决策未落库不得发送恢复命令。
        """
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(AgentApproval).where(AgentApproval.run_id == run_id).order_by(AgentApproval.id)
                )).scalars().all()
                for i, dec in enumerate(decisions):
                    row = self._match_approval_row(rows, dec, i)
                    if row is None:
                        raise ApprovalInvalid(f"未知审批动作: {dec.get('action_id') or f'#{i + 1}'}")
                    if row.decision is not None:
                        if row.decision != dec.get("type"):
                            raise ApprovalConflict(
                                f"审批动作 {row.action_id or row.id} 已决策为 {row.decision}，"
                                f"与新决策 {dec.get('type')} 冲突"
                            )
                        continue  # 重复一致决策：幂等，不重复写入
                    row.decision = str(dec.get("type"))
                    row.message = dec.get("message")
                    row.decided_by = user_id
                    row.decided_at = datetime.utcnow()
                    row.decision_status = "approved" if dec.get("type") == "approve" else "rejected"
                await db.commit()
        except (ApprovalInvalid, ApprovalConflict):
            raise
        except Exception as e:
            raise PersistenceError(f"run {run_id} 审批决策写入失败: {e}") from e


class _RunLimitExceeded(Exception):
    pass


# 摘要纳入的 Agent 配置版本字段（执行语义相关，必要且稳定）；
# 易变的服务端临时字段（如检索到的记忆上下文、预加载表列表）与明文凭据不纳入
_AGENT_CONFIG_VERSION_KEYS = (
    "id",
    "model_config_id",
    "system_prompt",
    "max_iterations",
    "max_tokens",
    "temperature",
    "tools",
    "knowledge_base_id",
    "database_connection_id",
)


def compute_request_hash(
    *,
    endpoint: str,
    user_id: Optional[int],
    agent_id: Any,
    session_id: Optional[int],
    user_input: str,
    agent_config: Optional[Dict[str, Any]],
) -> str:
    """规范化请求摘要（P1-01）。

    至少包含：user_id、agent_id、session_id、endpoint/操作类型、用户本次输入、
    必要的 Agent 配置版本。服务端易变临时字段（``_`` 前缀 / 记忆检索上下文 /
    预加载表列表）与明文凭据不纳入摘要。
    """
    canonical = json.dumps(
        {
            "endpoint": endpoint,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "user_input": user_input,
            "config_version": {
                k: (agent_config or {}).get(k) for k in _AGENT_CONFIG_VERSION_KEYS
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_user_input(messages: List[Dict[str, Any]]) -> str:
    """取用户本次输入（最后一条 user 消息内容），作为请求摘要的输入项。"""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content") or "")
    return str((messages[-1].get("content") or "")) if messages else ""


_service: Optional[AgentRuntimeService] = None


def get_runtime_service() -> AgentRuntimeService:
    global _service
    if _service is None:
        _service = AgentRuntimeService()
    return _service

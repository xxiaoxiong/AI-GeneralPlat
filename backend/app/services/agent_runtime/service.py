"""
AgentRuntimeService：平台唯一 Agent 执行入口（DeepAgents + LangGraph）。

职责：
- 创建/驱动 Run（AsyncIO task + 事件总线）
- 模型/工具/预算/安全策略组装（factory / tools / model_adapter）
- Checkpoint 持久化与 thread 管理（persistence）
- HITL 中断→审批→恢复（interrupt / Command(resume)）
- 取消、超时、预算超限的确定性状态
- Run/Approval 记录落库（agent_runs / agent_approvals）
"""
from __future__ import annotations

import asyncio
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
from app.core.database import AsyncSessionLocal

from .context import ExecutionBudget, RuntimeContext, RunScope, messages_to_langchain
from .events import (
    APPROVAL_REQUIRED,
    MODEL_DELTA,
    PLAN_UPDATED,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    RUN_WAITING_APPROVAL,
    SUBAGENT_COMPLETED,
    SUBAGENT_STARTED,
    TOOL_COMPLETED,
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
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    RunHandle,
    RunRegistry,
)
from .model_adapter import ModelCapabilityError
from .persistence import CheckpointerFactory, make_run_config, make_thread_id
from .telemetry import telemetry
from .tools.policy import ToolPolicy, ToolPolicyError

logger = logging.getLogger("agent_runtime")


class AgentRuntimeService:
    """统一 Agent 执行服务（单例使用）。"""

    def __init__(self) -> None:
        self.registry = RunRegistry.instance()

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
    ) -> Tuple[str, AsyncGenerator[Dict[str, Any], None]]:
        """启动运行并返回 (run_id, 事件流)：run_id 可先行写入响应头（X-Run-Id）。"""
        run, adapter = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
        )

        async def _gen() -> AsyncGenerator[Dict[str, Any], None]:
            async for ev in run.bus.events():
                for legacy in adapter.adapt(ev):
                    yield legacy

        return run.run_id, _gen()

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
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """非流式执行：返回 {status, content, steps, run_id}。"""
        run, adapter = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            use_memory_context=use_memory_context,
            db_info=db_info,
        )
        steps: List[Dict[str, Any]] = []
        async for ev in run.bus.events():
            steps.extend(adapter.adapt(ev))
        content = "".join(adapter.final_text_parts)
        return {
            "status": run.status,
            "content": content,
            "steps": [s for s in steps if s.get("type") != "done"],
            "run_id": run.run_id,
        }

    async def resume_run(
        self,
        run_id: str,
        decisions: List[Dict[str, Any]],
        deciding_user_id: int,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """HITL 恢复：decisions=[{"type":"approve"|"reject","message":...}, ...]。"""
        run = self.registry.get(run_id)
        if run is None or run.status != STATUS_WAITING_APPROVAL:
            yield {"type": "error", "content": "运行不存在或不在待审批状态"}
            yield {"type": "done", "status": "failed"}
            return

        adapter = LegacyEventAdapter(run_id)
        await self._record_approval_decision(run_id, decisions, deciding_user_id)
        run.transition(STATUS_RUNNING)
        await self._persist_run(run)

        run.bus = EventBus()
        run.bus.bind_run(thread_id=run.thread_id, agent_id=run.agent_id, trace_id=run.run_id)
        run.task = asyncio.create_task(
            self._drive(run, adapter, messages=None, resume={"decisions": decisions})
        )
        async for ev in run.bus.events():
            for legacy in adapter.adapt(ev):
                yield legacy

    async def cancel_run(self, run_id: str, user_id: int) -> Dict[str, Any]:
        run = self.registry.get(run_id)
        if run is None:
            return {"ok": False, "error": "运行不存在"}
        if run.user_id != user_id:
            return {"ok": False, "error": "无权取消该运行"}
        if run.is_terminal:
            return {"ok": True, "status": run.status}
        if run.task and not run.task.done():
            run.task.cancel()
            return {"ok": True, "status": "cancelling"}
        # 等待审批的运行直接置为取消
        run.transition(STATUS_CANCELLED)
        await self._persist_run(run)
        return {"ok": True, "status": run.status}

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
        return {
            "thread_id": thread_id,
            "checkpoint_id": getattr(snapshot, "id", None),
            "next": list(getattr(snapshot, "next", ()) or ()),
            "has_pending_writes": bool(getattr(snapshot, "pending_writes", None)),
        }

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
    ) -> Tuple[RunHandle, LegacyEventAdapter]:
        thread_id = make_thread_id(agent_config["id"], session_id, uuid.uuid4().hex)
        run = await self.registry.create(
            agent_id=agent_config["id"],
            session_id=session_id,
            user_id=user_id,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
        )
        if run.task is not None:
            # 幂等命中：复用已有运行
            return run, LegacyEventAdapter(run.run_id)

        adapter = LegacyEventAdapter(run.run_id)
        run.transition(STATUS_RUNNING)
        await self._persist_run(run)
        run.task = asyncio.create_task(
            self._drive(
                run,
                adapter,
                messages=messages,
                resume=None,
                agent_config=agent_config,
                use_memory_context=use_memory_context,
                db_info=db_info,
                user_id=user_id,
            )
        )
        return run, adapter

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
                else:
                    payload = {"messages": messages_to_langchain(messages or []), **skill_files}

                config = make_run_config(run.thread_id, run.run_id, scope.user_id)
                config["recursion_limit"] = ctx.budget.max_steps * 3 + 10
                run_config = config

                try:
                    async with asyncio.timeout(ctx.budget.run_timeout_seconds):
                        await self._consume_stream(agent, payload, run_config, ctx, run, bus)
                except asyncio.TimeoutError:
                    raise _RunLimitExceeded(f"运行超时（>{ctx.budget.run_timeout_seconds}s）")

            # 结束分支
            if run.status == STATUS_RUNNING:
                run.final_text = "".join(adapter.final_text_parts)
                run.summary = {
                    "tool_calls": ctx.tool_calls,
                    "model_calls": ctx.model_calls,
                    "elapsed_seconds": round(ctx.elapsed(), 2),
                }
                run.transition(STATUS_COMPLETED)
                await bus.publish(RuntimeEvent(
                    type=RUN_COMPLETED,
                    run_id=run.run_id,
                    data={"final_text": run.final_text, "summary": run.summary},
                ))

        except asyncio.CancelledError:
            if not run.is_terminal:
                try:
                    run.transition(STATUS_CANCELLED)
                except Exception:
                    pass
            await self._persist_run(run)
            await bus.publish(RuntimeEvent(type=RUN_CANCELLED, run_id=run.run_id))
            raise
        except _RunLimitExceeded as e:
            await self._fail(run, str(e), status=STATUS_LIMIT_EXCEEDED, bus=bus)
        except (ToolCallLimitExceededError, ModelCallLimitExceededError, GraphRecursionError) as e:
            # 执行预算超限（工具/模型次数、步数）→ 明确 limit_exceeded 状态
            await self._fail(run, f"执行预算超限: {e}", status=STATUS_LIMIT_EXCEEDED, bus=bus)
        except ModelCapabilityError as e:
            await self._fail(run, f"模型能力不满足: {e}", status=STATUS_FAILED, bus=bus)
        except ToolPolicyError as e:
            await self._fail(run, f"安全策略拒绝: {e}", status=STATUS_FAILED, bus=bus)
        except Exception as e:
            logger.exception("run %s 执行失败", run.run_id)
            await self._fail(run, str(e), status=STATUS_FAILED, bus=bus)
        finally:
            await self._persist_run(run)
            telemetry.record_run(
                log_fields,
                status=run.status,
                duration_ms=round((ctx.elapsed() if ctx else 0.0) * 1000, 1) or None,
                error=run.error,
                attributes=run.summary or {},
            )

    async def _consume_stream(self, agent, payload, run_config, ctx, run, bus) -> None:
        """消费 astream：模型增量 / 计划 / 中断 -> 事件总线。"""
        interrupted = False
        async for mode, chunk in agent.astream(
            payload, config=run_config, stream_mode=["updates", "messages"]
        ):
            if mode == "messages":
                msg, meta = chunk
                if isinstance(msg, (AIMessage, AIMessageChunk)) and meta.get("langgraph_node") == "model":
                    # 模型增量（含非流式整段消息；仅主模型节点可见文本）
                    text = msg.content if isinstance(msg.content, str) else ""
                    if text:
                        ctx.model_calls += 1
                        await bus.publish(RuntimeEvent(
                            type=MODEL_DELTA, run_id=run.run_id, data={"text": text}
                        ))
            elif mode == "updates":
                if not isinstance(chunk, dict):
                    continue
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
            value = getattr(it, "value", it)
            if isinstance(value, dict) and "action_requests" in value:
                for req in value["action_requests"]:
                    actions.append({
                        "tool": req.get("name") or req.get("action") or req.get("tool"),
                        "args": redact_sensitive(req.get("args") or {}),
                        "description": req.get("description", ""),
                    })
        run.pending_actions = actions
        await self._record_approval_requests(run.run_id, actions)
        await bus.publish(RuntimeEvent(
            type=APPROVAL_REQUIRED, run_id=run.run_id, data={"actions": actions}
        ))

    async def _fail(self, run: RunHandle, error: str, *, status: str, bus: EventBus) -> None:
        run.error = error
        try:
            run.transition(status)
        except Exception:
            pass
        await bus.publish(RuntimeEvent(
            type=RUN_FAILED, run_id=run.run_id,
            data={"error": error, "status": status},
        ))

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

    # ── 内部：持久化 ──────────────────────────────────────────────────────

    async def _persist_run(self, run: RunHandle) -> None:
        try:
            from sqlalchemy import select
            from app.models.agent import AgentRun

            async with AsyncSessionLocal() as db:
                row = (await db.execute(select(AgentRun).where(AgentRun.id == run.run_id))).scalar_one_or_none()
                if row is None:
                    row = AgentRun(
                        id=run.run_id, agent_id=run.agent_id, session_id=run.session_id,
                        user_id=run.user_id, thread_id=run.thread_id,
                    )
                    db.add(row)
                row.status = run.status
                row.error = run.error
                row.final_text = run.final_text or None
                row.summary = run.summary or None
                if run.is_terminal:
                    row.finished_at = datetime.utcnow()
                await db.commit()
        except Exception:
            logger.exception("run %s 状态持久化失败", run.run_id)

    async def _record_approval_requests(self, run_id: str, actions: List[Dict[str, Any]]) -> None:
        try:
            from app.models.agent import AgentApproval

            async with AsyncSessionLocal() as db:
                for a in actions:
                    db.add(AgentApproval(
                        run_id=run_id,
                        tool_name=str(a.get("tool") or "unknown"),
                        action_summary=json.dumps(a.get("args") or {}, ensure_ascii=False)[:2000],
                    ))
                await db.commit()
        except Exception:
            logger.exception("审批记录写入失败")

    async def _record_approval_decision(self, run_id: str, decisions: List[Dict[str, Any]], user_id: int) -> None:
        try:
            from sqlalchemy import select
            from app.models.agent import AgentApproval

            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(AgentApproval).where(AgentApproval.run_id == run_id, AgentApproval.decision.is_(None))
                )).scalars().all()
                for row, dec in zip(rows, decisions):
                    row.decision = dec.get("type", "approve")
                    row.message = dec.get("message")
                    row.decided_by = user_id
                    row.decided_at = datetime.utcnow()
                await db.commit()
        except Exception:
            logger.exception("审批决策写入失败")


class _RunLimitExceeded(Exception):
    pass


_service: Optional[AgentRuntimeService] = None


def get_runtime_service() -> AgentRuntimeService:
    global _service
    if _service is None:
        _service = AgentRuntimeService()
    return _service

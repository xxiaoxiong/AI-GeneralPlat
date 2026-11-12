"""
运行事件模型与旧 SSE 契约适配层。

内部统一使用结构化 RuntimeEvent；对外（AgentChat.vue / 老客户端）通过
LegacyEventAdapter 映射为旧事件流：
  thought / action / observation / final_start / final_chunk / final /
  error / verify / clarify / trace / done

映射规则：
  run_started        -> trace(run_started)
  plan / todo 变更    -> thought
  tool_started       -> action
  tool_completed     -> observation
  tool_failed        -> observation（含错误标记）
  model_delta        -> 首个 delta 前发 final_start，随后 final_chunk
  subagent_*         -> trace
  approval_required  -> 新事件 approval（旧客户端忽略），并附 thought 说明
  run_completed      -> final(全部可见模型文本拼接) + done
  run_failed         -> error + done
  run_cancelled      -> error(cancelled) + done
  run_suspended      -> done(status=suspended)（挂起边界：流必须结束，禁止悬挂等待）
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional


# ── 结构化运行事件 ────────────────────────────────────────────────────────────

RUN_STARTED = "run_started"
MODEL_DELTA = "model_delta"
TOOL_STARTED = "tool_started"
TOOL_COMPLETED = "tool_completed"
TOOL_FAILED = "tool_failed"
SUBAGENT_STARTED = "subagent_started"
SUBAGENT_COMPLETED = "subagent_completed"
APPROVAL_REQUIRED = "approval_required"
RUN_WAITING_APPROVAL = "run_waiting_approval"
STATE_CHANGED = "state_changed"
RUN_COMPLETED = "run_completed"
RUN_FAILED = "run_failed"
RUN_CANCELLED = "run_cancelled"
RUN_SUSPENDED = "run_suspended"
PLAN_UPDATED = "plan_updated"

# 流边界事件：消费者收到即结束本次事件流（Run 可能稍后由其他 Worker 恢复，
# 恢复后的执行产生新的事件流）。任务书 §12.2：SSE 消费者必须能终止。
TERMINAL_EVENTS = (
    RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED, RUN_WAITING_APPROVAL, RUN_SUSPENDED,
)


@dataclass
class RuntimeEvent:
    """平台标准运行事件（最小字段对齐任务书 §7.2）。

    sequence / thread_id / agent_id / status 由 EventBus 发布时补齐。
    """

    type: str
    run_id: str
    data: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    sequence: int = 0
    thread_id: str = ""
    agent_id: Optional[int] = None
    status: str = ""
    trace_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        from datetime import datetime, timezone

        return {
            "event_id": self.event_id,
            "event_type": self.type,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "timestamp": datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "sequence": self.sequence,
            "status": self.status,
            "payload": self.data,
            "trace_id": self.trace_id or self.run_id,
        }


def redact_sensitive(value: Any, max_len: int = 400) -> Any:
    """对外事件脱敏：截断长参数并遮蔽疑似密钥字段。"""
    sensitive_keys = {
        "api_key", "apikey", "authorization", "token", "secret",
        "password", "access_key", "private_key", "cookie",
    }
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k.lower() in sensitive_keys:
                out[k] = "***"
            else:
                out[k] = redact_sensitive(v, max_len)
        return out
    if isinstance(value, list):
        return [redact_sensitive(v, max_len) for v in value[:20]]
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + f"...[truncated {len(value) - max_len} chars]"
    return value


# ── 旧 SSE 契约适配 ──────────────────────────────────────────────────────────

class LegacyEventAdapter:
    """把 RuntimeEvent 流映射为旧 SSE 扁平事件 dict（前端契约：type/content/tool/input）。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.final_text_parts: List[str] = []
        self.final_started = False
        self.finished = False
        self.seq = 0  # 事件序号：SSE 客户端可据此检测丢事件/乱序

    def adapt(self, ev: RuntimeEvent) -> List[Dict[str, Any]]:
        """返回 0..n 个旧事件 dict，每个事件带自增 seq 序号。"""
        out = self._adapt(ev)
        for item in out:
            item["seq"] = self.seq
            self.seq += 1
        return out

    def _adapt(self, ev: RuntimeEvent) -> List[Dict[str, Any]]:
        """返回 0..n 个旧事件 dict。"""
        if self.finished:
            return []
        t = ev.type
        if t == RUN_STARTED:
            return []

        if t == PLAN_UPDATED:
            todos = ev.data.get("todos") or []
            lines = []
            for item in todos:
                status = item.get("status", "pending")
                mark = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(status, "[ ]")
                lines.append(f"{mark} {item.get('content', '')}")
            if lines:
                return [{"type": "thought", "content": "任务计划：\n" + "\n".join(lines)}]
            return []

        if t == TOOL_STARTED:
            out = self._ensure_final_start()
            out.append({
                "type": "action",
                "tool": ev.data.get("tool"),
                "input": redact_sensitive(ev.data.get("input")),
            })
            return out

        if t == TOOL_COMPLETED:
            return [{
                "type": "observation",
                "tool": ev.data.get("tool"),
                "content": redact_sensitive(ev.data.get("output"), max_len=1500),
            }]

        if t == TOOL_FAILED:
            return [{
                "type": "observation",
                "tool": ev.data.get("tool"),
                "content": f"工具执行失败: {ev.data.get('error')}",
                "error": True,
            }]

        if t == MODEL_DELTA:
            text = ev.data.get("text") or ""
            if not text:
                return []
            self.final_text_parts.append(text)
            out = self._ensure_final_start()
            out.append({"type": "final_chunk", "content": text})
            return out

        if t in (SUBAGENT_STARTED, SUBAGENT_COMPLETED):
            return [{
                "type": "thought",
                "content": f"子代理 {ev.data.get('name')} {'开始执行' if t == SUBAGENT_STARTED else '执行完成'}",
            }]

        if t == APPROVAL_REQUIRED:
            tools = [a.get("tool") for a in (ev.data.get("actions") or [])]
            return [{
                "type": "thought",
                "content": f"⏸ 等待人工审批：{', '.join(map(str, tools))}",
                "approval": {"run_id": self.run_id, "actions": redact_sensitive(ev.data.get("actions"))},
            }]

        if t == STATE_CHANGED:
            return []

        if t == RUN_WAITING_APPROVAL:
            out = self._ensure_final_start()
            out.append({"type": "done", "status": "waiting_approval", "run_id": self.run_id})
            self.finished = True
            return out

        if t == RUN_COMPLETED:
            final_text = ev.data.get("final_text")
            if final_text is None:
                final_text = "".join(self.final_text_parts)
            out = self._ensure_final_start()
            out.append({"type": "final", "content": final_text})
            out.append({"type": "done", "status": "completed"})
            self.finished = True
            return out

        if t == RUN_FAILED:
            out = self._ensure_final_start()
            out.append({"type": "error", "content": ev.data.get("error") or "运行失败"})
            out.append({"type": "done", "status": ev.data.get("status", "failed")})
            self.finished = True
            return out

        if t == RUN_CANCELLED:
            out = self._ensure_final_start()
            out.append({"type": "error", "content": "运行已取消"})
            out.append({"type": "done", "status": "cancelled"})
            self.finished = True
            return out

        if t == RUN_SUSPENDED:
            # 挂起边界（租约丢失/优雅停机/驱动异常结束）：流必须结束，
            # 消费者可按 run_id 重新订阅或走恢复流程，禁止无限等待
            out = self._ensure_final_start()
            out.append({
                "type": "done",
                "status": "suspended",
                "run_id": self.run_id,
                "reason": ev.data.get("reason"),
            })
            self.finished = True
            return out

        return []

    def _ensure_final_start(self) -> List[Dict[str, Any]]:
        if self.final_started:
            return []
        self.final_started = True
        return [{"type": "final_start"}]


def sse_format(data: Dict[str, Any]) -> str:
    """渲染为旧前端契约的 SSE wire 格式（data: {json} 行）。"""
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


class EventBus:
    """run 级事件总线：异步队列 + 订阅广播（当前单订阅者场景直接出队）。

    发布时为事件补齐 §7.2 最小字段：sequence（单调解号）、thread_id、
    agent_id、status（当前 run 状态）、trace_id。

    Phase 5（FR-07）持久化接线：persist=True 的 run 级总线在发布时先把
    事件写入 agent_run_events（数据库是事实来源，sequence 由
    agent_runs.last_event_seq 原子自增分配，payload 入库前脱敏），
    成功后才通过内存队列唤醒本地 SSE 消费者 —— 内存队列只是通知优化，
    溢出时仅丢通知、绝不丢持久化事件；事件写入失败显式抛出
    PersistenceError，不得静默当作成功。
    """

    def __init__(self, maxsize: int = 1000, *, persist: bool = False):
        import asyncio

        self._queue: "asyncio.Queue[RuntimeEvent]" = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self._seq = 0
        self.persist = persist
        self.thread_id = ""
        self.agent_id: Optional[int] = None
        self.status = ""
        self.trace_id = ""

    def bind_run(self, *, thread_id: str, agent_id: Optional[int], trace_id: str = "") -> None:
        """绑定 run 级上下文（由 RunHandle 构造时调用）。"""
        self.thread_id = thread_id
        self.agent_id = agent_id
        self.trace_id = trace_id

    @staticmethod
    async def _persist(ev: RuntimeEvent) -> int:
        """持久化事件并返回分配的 run 内序号（失败向上抛出 PersistenceError）。"""
        from app.core.database import AsyncSessionLocal
        from .repositories import EventRepository, PersistenceError

        # 脱敏入库：密钥遮蔽一律保留；文本型事件（模型增量/最终结果）不截断，
        # 否则回放/最终文本重建会静默丢失文本（FR-09.5）
        max_len = 100_000 if ev.type in (MODEL_DELTA, RUN_COMPLETED) else 400
        payload = redact_sensitive(ev.data or {}, max_len=max_len)
        try:
            async with AsyncSessionLocal() as db:
                seq = await EventRepository(db).append(
                    ev.run_id, ev.type, payload,
                    thread_id=ev.thread_id or "",
                )
                await db.commit()
            return seq
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(f"run {ev.run_id} 事件持久化失败: {e}") from e

    async def publish(self, ev: RuntimeEvent) -> None:
        if self._closed:
            return
        ev.thread_id = ev.thread_id or self.thread_id
        ev.agent_id = ev.agent_id if ev.agent_id is not None else self.agent_id
        ev.status = ev.status or self.status
        ev.trace_id = ev.trace_id or self.trace_id
        if self.persist and ev.run_id:
            # FR-07：先持久化（可靠序号），再通知本地消费者
            ev.sequence = await self._persist(ev)
        else:
            self._seq += 1
            ev.sequence = self._seq
        try:
            self._queue.put_nowait(ev)
        except Exception:
            # 队列满时丢弃最旧事件，保证执行不被观测阻塞（持久化事件仍在 DB）
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(ev)
            except Exception:
                pass

    async def events(self) -> AsyncGenerator[RuntimeEvent, None]:
        while True:
            ev = await self._queue.get()
            yield ev
            if ev.type in TERMINAL_EVENTS:
                break

    async def get(self, timeout: Optional[float] = None) -> Optional[RuntimeEvent]:
        """取下一个事件；指定 timeout 时超时返回 None。

        供消费端在无事件间隙做"生产侧是否已结束"的判定，避免无限悬挂等待。
        被取消/超时的 getter 不会丢失队列中的事件（asyncio.Queue 语义保证）。
        """
        import asyncio

        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def empty(self) -> bool:
        return self._queue.empty()

    def close(self) -> None:
        self._closed = True

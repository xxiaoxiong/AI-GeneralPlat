"""
Run 生命周期：状态机、幂等、并发安全。

状态转移（单向 + 少量受控转移）：
  pending -> running -> completed / failed / cancelled / limit_exceeded
  running -> waiting_approval -> running（恢复后）/ cancelled / failed
  waiting_approval 是唯一可恢复的暂停态

进程内注册表保存活跃 run 句柄（含 asyncio.Task）；持久态在 agent_runs 表。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from .events import EventBus

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_WAITING_APPROVAL = "waiting_approval"
STATUS_NEEDS_RECONCILIATION = "needs_reconciliation"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_LIMIT_EXCEEDED = "limit_exceeded"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_LIMIT_EXCEEDED}

# 暂停态：可被人工恢复，不会被 Worker 自动领取执行
PAUSED_STATUSES = {STATUS_WAITING_APPROVAL, STATUS_NEEDS_RECONCILIATION}

ALLOWED_TRANSITIONS: Dict[str, Set[str]] = {
    STATUS_PENDING: {STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED},
    STATUS_RUNNING: {
        STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED,
        STATUS_LIMIT_EXCEEDED, STATUS_WAITING_APPROVAL,
        STATUS_NEEDS_RECONCILIATION,
    },
    STATUS_WAITING_APPROVAL: {STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED},
    # 人工核对后可显式继续/终止；不得自动执行
    STATUS_NEEDS_RECONCILIATION: {STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED},
    STATUS_COMPLETED: set(),
    STATUS_FAILED: set(),
    STATUS_CANCELLED: set(),
    STATUS_LIMIT_EXCEEDED: set(),
}


class InvalidTransition(Exception):
    pass


@dataclass
class RunHandle:
    run_id: str
    agent_id: int
    session_id: Optional[int]
    user_id: int
    thread_id: str
    status: str = STATUS_PENDING
    error: Optional[str] = None
    idempotency_key: Optional[str] = None
    final_text: str = ""
    # FR-09：最终文本权威收集（执行侧 _consume_stream 累积；事件适配器不承担收集）
    final_text_parts: List[str] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    pending_actions: list = field(default_factory=list)
    bus: EventBus = field(default_factory=lambda: EventBus(persist=True))
    task: Optional[asyncio.Task] = None
    # ── Durable Execution（Phase 2）：租约与执行尝试 ─────────────────────
    lease_version: Optional[int] = None   # 当前持有租约的 fencing token（未持有时为 None）
    worker_id: Optional[str] = None       # 当前租约持有者
    attempt: int = 0                      # 执行尝试次数（与 agent_runs.attempt 同步）
    # 挂起请求：优雅关闭 / 租约丢失时置位——取消执行但不写 cancelled，
    # 保留 running 状态供其他 Worker 经 Checkpoint 恢复
    suspend_requested: bool = False
    suspend_reason: str = ""  # 挂起原因（lease_lost / shutdown），随 run_suspended 事件通知消费者
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        # 事件总线绑定 run 上下文，发布事件时补齐 thread_id/agent_id/status/trace_id
        self.bus.bind_run(thread_id=self.thread_id, agent_id=self.agent_id, trace_id=self.run_id)
        self.bus.status = self.status

    def transition(self, new_status: str) -> None:
        if new_status not in ALLOWED_TRANSITIONS.get(self.status, set()):
            raise InvalidTransition(f"run {self.run_id}: {self.status} -> {new_status} 不允许")
        self.status = new_status
        self.updated_at = datetime.utcnow()
        self.bus.status = new_status

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class RunRegistry:
    """进程内 run 注册表（跨进程状态以 agent_runs 表 + checkpoint 为准）。"""

    _instance: Optional["RunRegistry"] = None

    def __init__(self) -> None:
        self._runs: Dict[str, RunHandle] = {}
        self._by_idem: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> "RunRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def create(
        self,
        *,
        agent_id: int,
        session_id: Optional[int],
        user_id: int,
        thread_id: str,
        idempotency_key: Optional[str] = None,
    ) -> RunHandle:
        async with self._lock:
            if idempotency_key:
                existing_id = self._by_idem.get(idempotency_key)
                if existing_id and existing_id in self._runs:
                    existing = self._runs[existing_id]
                    if not existing.is_terminal:
                        return existing
            run = RunHandle(
                run_id=uuid.uuid4().hex,
                agent_id=agent_id,
                session_id=session_id,
                user_id=user_id,
                thread_id=thread_id,
                idempotency_key=idempotency_key,
            )
            self._runs[run.run_id] = run
            if idempotency_key:
                self._by_idem[idempotency_key] = run.run_id
            return run

    def get(self, run_id: str) -> Optional[RunHandle]:
        return self._runs.get(run_id)

    def attach(
        self,
        *,
        run_id: str,
        agent_id: int,
        session_id: Optional[int],
        user_id: Optional[int],
        thread_id: str,
        idempotency_key: Optional[str] = None,
        status: str = STATUS_PENDING,
    ) -> RunHandle:
        """把数据库中的 Run 挂载为进程内句柄（幂等：已存在则复用）。

        与 ``create`` 不同：run_id 由数据库侧生成/提供，Registry 只做本地缓存。
        """
        existing = self._runs.get(run_id)
        if existing is not None:
            return existing
        handle = RunHandle(
            run_id=run_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id or 0,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            status=status,
        )
        self._runs[run_id] = handle
        if idempotency_key:
            self._by_idem[idempotency_key] = run_id
        return handle

    def release(self, run_id: str) -> None:
        """终态 run 移出活跃表（保留记录在 DB）。"""
        run = self._runs.get(run_id)
        if run and run.is_terminal and run.task is None or (run and run.is_terminal and run.task and run.task.done()):
            self._runs.pop(run_id, None)

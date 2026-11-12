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
from typing import Any, Dict, Optional, Set

from .events import EventBus

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_WAITING_APPROVAL = "waiting_approval"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_LIMIT_EXCEEDED = "limit_exceeded"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_LIMIT_EXCEEDED}

ALLOWED_TRANSITIONS: Dict[str, Set[str]] = {
    STATUS_PENDING: {STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED},
    STATUS_RUNNING: {
        STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED,
        STATUS_LIMIT_EXCEEDED, STATUS_WAITING_APPROVAL,
    },
    STATUS_WAITING_APPROVAL: {STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED},
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
    summary: Dict[str, Any] = field(default_factory=dict)
    pending_actions: list = field(default_factory=list)
    bus: EventBus = field(default_factory=EventBus)
    task: Optional[asyncio.Task] = None
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

    def release(self, run_id: str) -> None:
        """终态 run 移出活跃表（保留记录在 DB）。"""
        run = self._runs.get(run_id)
        if run and run.is_terminal and run.task is None or (run and run.is_terminal and run.task and run.task.done()):
            self._runs.pop(run_id, None)

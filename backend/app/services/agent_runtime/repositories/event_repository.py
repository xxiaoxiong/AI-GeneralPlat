"""Run 事件仓库：agent_run_events 表（SSE 回放的持久化事实来源）。

设计（任务书 FR-07）：
- 序号由 ``agent_runs.last_event_seq`` 原子自增分配（并发安全）；
- 先持久化事件，再通过本地 EventBus 唤醒消费者 —— 数据库是事实来源，
  内存总线只是通知优化；
- payload 入库前必须已脱敏（调用方负责，本层再兜底不做二次加工）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun, AgentRunEvent
from ..lifecycle import TERMINAL_STATUSES
from .run_repository import RunRepository


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._runs = RunRepository(session)

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        thread_id: str = "",
    ) -> int:
        """持久化事件并返回分配的 sequence。失败向上抛出（F-17）。"""
        seq = await self._runs.next_event_seq(run_id)
        self.session.add(
            AgentRunEvent(
                run_id=run_id,
                sequence=seq,
                event_type=event_type,
                payload=payload,
                thread_id=thread_id[:64] or "",
            )
        )
        await self.session.flush()
        return seq

    async def list_after(
        self, run_id: str, after_seq: int, limit: Optional[int] = None
    ) -> List[AgentRunEvent]:
        """按 sequence 递增返回 ``sequence > after_seq`` 的事件（SSE 回放）。"""
        stmt = (
            select(AgentRunEvent)
            .where(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.sequence > after_seq,
            )
            .order_by(AgentRunEvent.sequence)
            .execution_options(populate_existing=True)
        )
        if limit:
            stmt = stmt.limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def last_seq(self, run_id: str) -> int:
        res = await self.session.execute(
            select(AgentRun.last_event_seq).where(AgentRun.id == run_id)
        )
        val = res.scalar_one_or_none()
        return val or 0

    async def cleanup(self, retention_days: int, batch: int = 500) -> int:
        """事件保留策略（§13/FR-07）：只清理终态且超期的 Run 事件。

        仍处于运行/暂停（waiting_approval / needs_reconciliation）的 Run
        事件一律不得清理。返回删除行数。
        """
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        stale = await self.session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.status.in_(tuple(TERMINAL_STATUSES)),
                AgentRun.finished_at.isnot(None),
                AgentRun.finished_at < cutoff,
            )
            .limit(batch)
        )
        run_ids = list(stale.scalars().all())
        if not run_ids:
            return 0
        res = await self.session.execute(
            delete(AgentRunEvent).where(AgentRunEvent.run_id.in_(run_ids))
        )
        await self.session.flush()
        return res.rowcount or 0

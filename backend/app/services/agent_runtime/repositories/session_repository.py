"""Session 仓库：消息写入（乐观锁）+ 执行权（原子 Claim）+ 结果写回 Outbox。

设计要点（任务书 P1-02）：
- **执行权**：``claim_execution`` 单条条件 UPDATE（CAS）原子获取，同一 Session
  同时只有一个活跃 Run；Worker 故障时租约过期可被接管；同 Run 重复 Claim 为
  刷新租约（幂等）。
- **消息写入**：每条消息带稳定 ``message_id``，Assistant 消息带 ``source_run_id``；
  写回使用 ``version`` 乐观锁 CAS（读取-合并-写回），冲突时基于最新内容合并
  重试，绝不盲目覆盖（并发写入不丢历史）；``source_run_id`` 已存在则跳过
  （写回幂等）。
- **Outbox**：``session_result_outbox`` 与 Run 终态同事务入队；写回与
  ``mark_delivered`` 同事务提交，重试不会重复追加 Assistant 消息。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentSession, SessionResultOutbox

from .run_repository import _sql_future, _sql_now

# 写回/追加的乐观锁重试上限（冲突时合并重试，不盲目覆盖）
_MERGE_RETRIES = 5

OUTBOX_PENDING = "pending"
OUTBOX_DELIVERED = "delivered"


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── 执行权（P1-02 A：单 Session 单活跃 Run） ─────────────────────────

    async def claim_execution(
        self, session_id: int, run_id: str, lease_seconds: int
    ) -> bool:
        """原子获取 Session 执行权。成功/同 Run 刷新租约返回 True。

        CAS 条件（缺一不可）：
        - Session 无持有者，或持有者就是本 Run（幂等刷新）；
        - 或原持有者租约已过期（Worker 故障过期接管）。
        其余情况返回 False（明确冲突，不得并发驱动同一 Session）。
        """
        res = await self.session.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                (
                    (AgentSession.active_run_id.is_(None))
                    | (AgentSession.active_run_id == run_id)
                    | (AgentSession.active_run_lease_expires_at.is_(None))
                    | (AgentSession.active_run_lease_expires_at < _sql_now(self.session))
                ),
            )
            .values(
                active_run_id=run_id,
                active_run_lease_expires_at=_sql_future(self.session, lease_seconds),
                updated_at=datetime.utcnow(),
            ),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    async def release_execution(self, session_id: int, run_id: str) -> bool:
        """释放 Session 执行权（仅持有者可释放）。"""
        res = await self.session.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.active_run_id == run_id,
            )
            .values(
                active_run_id=None,
                active_run_lease_expires_at=None,
                updated_at=datetime.utcnow(),
            ),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    async def is_execution_free(
        self, session_id: Optional[int], run_id: str
    ) -> bool:
        """Session 执行权是否可被 run_id 获取（无 Session 视为自由）。"""
        if session_id is None:
            return True
        res = await self.session.execute(
            select(AgentSession.id).where(
                AgentSession.id == session_id,
                (
                    (AgentSession.active_run_id.is_(None))
                    | (AgentSession.active_run_id == run_id)
                    | (AgentSession.active_run_lease_expires_at.is_(None))
                    | (AgentSession.active_run_lease_expires_at < _sql_now(self.session))
                ),
            )
        )
        return res.first() is not None

    # ── 消息写入（P1-02 B：稳定身份 + version 乐观锁 + 合并重试） ────────

    async def append_messages(
        self,
        session_id: int,
        new_messages: Sequence[Dict[str, Any]],
        *,
        source_run_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> int:
        """幂等追加消息并返回新 version。

        - 按 ``message_id`` 去重：已存在的消息不重复追加；
        - ``source_run_id`` 已在历史中出现时直接返回（Assistant 写回幂等）；
        - version CAS：冲突时重新读取最新内容合并重试，不盲目覆盖。
        """
        last_err: Optional[Exception] = None
        for _ in range(_MERGE_RETRIES):
            # with_for_update：当前读 + 行锁。MySQL REPEATABLE READ 下普通
            # SELECT 是快照读，CAS 失败后的重试会读到旧 version 导致永远不命中；
            # 行锁读取最新已提交行，读-合并-写整体原子（PG/SQLite 同样适用）
            res = await self.session.execute(
                select(AgentSession)
                .where(AgentSession.id == session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            sess = res.scalar_one_or_none()
            if sess is None:
                raise LookupError(f"session {session_id} 不存在")

            msgs = list(sess.messages or [])
            if source_run_id and any(
                isinstance(m, dict) and m.get("source_run_id") == source_run_id
                for m in msgs
            ):
                return sess.version or 0  # 已写回：幂等跳过

            existing_ids = {
                m.get("message_id") for m in msgs if isinstance(m, dict) and m.get("message_id")
            }
            to_add = [
                m for m in new_messages
                if not (isinstance(m, dict) and m.get("message_id") in existing_ids)
            ]

            values: Dict[str, Any] = {
                "messages": msgs + to_add,
                "version": (sess.version or 0) + 1,
                "updated_at": datetime.utcnow(),
            }
            # 标题仅在初始标题（空 / "新对话"）时更新，不覆盖用户已改标题
            if title and (not sess.title or sess.title == "新对话"):
                values["title"] = title
            res = await self.session.execute(
                update(AgentSession)
                .where(
                    AgentSession.id == session_id,
                    AgentSession.version == (sess.version or 0),
                )
                .values(**values),
                execution_options={"synchronize_session": False},
            )
            if res.rowcount == 1:
                return (sess.version or 0) + 1
            last_err = None  # CAS 未命中：基于最新内容合并重试
        raise RuntimeError(
            f"session {session_id} 消息写入冲突（{_MERGE_RETRIES} 次合并重试未命中）: {last_err}"
        )

    async def get_messages(self, session_id: int) -> List[Dict[str, Any]]:
        res = await self.session.execute(
            select(AgentSession.messages)
            .where(AgentSession.id == session_id)
            .execution_options(populate_existing=True)
        )
        row = res.first()
        return list(row[0] or []) if row else []


class SessionOutboxRepository:
    """Run 终态 → Session 写回 Outbox（P1-02 C）。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(self, run_id: str, session_id: int) -> bool:
        """入队写回任务（幂等：同一 run_id 只入队一次，与 Run 终态同事务调用）。"""
        existing = await self.get_by_run(run_id)
        if existing is not None:
            return False
        self.session.add(SessionResultOutbox(
            run_id=run_id,
            session_id=session_id,
            status=OUTBOX_PENDING,
            attempts=0,
        ))
        return True

    async def get_by_run(self, run_id: str) -> Optional[SessionResultOutbox]:
        res = await self.session.execute(
            select(SessionResultOutbox)
            .where(SessionResultOutbox.run_id == run_id)
            .execution_options(populate_existing=True)
        )
        return res.scalar_one_or_none()

    async def list_pending(self, limit: int = 20) -> List[SessionResultOutbox]:
        res = await self.session.execute(
            select(SessionResultOutbox)
            .where(SessionResultOutbox.status == OUTBOX_PENDING)
            .order_by(SessionResultOutbox.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list(res.scalars().all())

    async def mark_delivered(self, run_id: str) -> None:
        await self.session.execute(
            update(SessionResultOutbox)
            .where(SessionResultOutbox.run_id == run_id)
            .values(
                status=OUTBOX_DELIVERED,
                delivered_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ),
            execution_options={"synchronize_session": False},
        )

    async def record_failure(self, run_id: str, error: str) -> None:
        """记录一次失败尝试（重试由 Outbox Worker 的下一轮扫描驱动）。"""
        await self.session.execute(
            update(SessionResultOutbox)
            .where(SessionResultOutbox.run_id == run_id)
            .values(
                attempts=SessionResultOutbox.attempts + 1,
                last_error=(error or "")[:2000],
                updated_at=datetime.utcnow(),
            ),
            execution_options={"synchronize_session": False},
        )

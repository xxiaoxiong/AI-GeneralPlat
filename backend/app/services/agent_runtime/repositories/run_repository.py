"""Run 仓库：agent_runs 表的唯一读写入口。

设计要点（任务书 FR-01 / FR-02 / 原则 A、C）：
- 创建与幂等由数据库唯一约束 ``uq_agent_runs_idem`` 保证，不依赖内存 Registry；
- Worker 执行权由原子条件 UPDATE（CAS）获得，同一 Run 同时只有一个有效 Lease；
- ``lease_version`` 作为 fencing token：每次 Claim/Release 递增，旧 Worker
  的状态提交因版本不匹配被拒绝；
- 所有持久化失败向上抛出（IntegrityError/DBAPIError 不吞）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun
from ..lifecycle import (
    STATUS_PENDING,
    STATUS_RUNNING,
    PAUSED_STATUSES,
    TERMINAL_STATUSES,
)
from .errors import IdempotencyConflict

# claim_next 可领取的状态：pending，或 Lease 过期的 running（进入恢复检查）
_CLAIMABLE = (STATUS_PENDING, STATUS_RUNNING)


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── 创建 / 查询（FR-01） ────────────────────────────────────────────

    async def create_run(
        self,
        *,
        run_id: str,
        agent_id: int,
        session_id: Optional[int],
        user_id: Optional[int],
        thread_id: str,
        idempotency_key: Optional[str] = None,
        request_hash: Optional[str] = None,
        input_payload: Optional[Dict[str, Any]] = None,
        agent_config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Tuple[AgentRun, bool]:
        """创建 Run。返回 (run, created)。

        - 同一幂等键、同一请求摘要：返回已有 Run（created=False）；
        - 同一幂等键、不同请求摘要：抛 IdempotencyConflict；
        - 未提供幂等键：每次创建新 Run。
        """
        if idempotency_key:
            existing = await self.get_by_idempotency_key(user_id, idempotency_key)
            if existing is not None:
                return self._check_reuse(existing, request_hash), False

        run = AgentRun(
            id=run_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            thread_id=thread_id,
            status=STATUS_PENDING,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            input_payload=input_payload,
            agent_config_snapshot=agent_config_snapshot,
        )
        self.session.add(run)
        try:
            await self.session.flush()
        except IntegrityError:
            # 并发下另一请求先创建了同一幂等键的 Run：回滚后复用
            await self.session.rollback()
            existing = await self.get_by_idempotency_key(user_id, idempotency_key)
            if existing is None:
                raise
            return self._check_reuse(existing, request_hash), False
        return run, True

    @staticmethod
    def _check_reuse(existing: AgentRun, request_hash: Optional[str]) -> AgentRun:
        if (
            request_hash
            and existing.request_hash
            and existing.request_hash != request_hash
        ):
            raise IdempotencyConflict(
                f"幂等键 {existing.idempotency_key} 已用于不同请求内容"
            )
        return existing

    async def get_run(self, run_id: str) -> Optional[AgentRun]:
        # populate_existing：数据库是事实来源，不信任 ORM 身份映射的旧值
        res = await self.session.execute(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
        )
        return res.scalar_one_or_none()

    async def get_by_idempotency_key(
        self, user_id: Optional[int], idempotency_key: str
    ) -> Optional[AgentRun]:
        res = await self.session.execute(
            select(AgentRun)
            .where(
                AgentRun.user_id == user_id,
                AgentRun.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
        return res.scalar_one_or_none()

    # ── 状态更新（fencing：lease_version 匹配才允许提交） ────────────────

    async def update_status(
        self,
        run_id: str,
        *,
        status: str,
        lease_version: Optional[int] = None,
        error: Optional[str] = None,
        final_text: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        recovery_error: Optional[str] = None,
        finished: bool = False,
    ) -> bool:
        """提交状态。传入 lease_version 时执行 fencing 校验；返回是否提交成功。

        F-13：租约过期后的旧 Worker 提交必须失败（rowcount=0）。
        """
        values: Dict[str, Any] = {"status": status, "updated_at": datetime.utcnow()}
        if error is not None:
            values["error"] = error
        if final_text is not None:
            values["final_text"] = final_text
        if summary is not None:
            values["summary"] = summary
        if recovery_error is not None:
            values["recovery_error"] = recovery_error
        if finished:
            values["finished_at"] = datetime.utcnow()

        conditions = [AgentRun.id == run_id]
        if lease_version is not None:
            conditions.append(AgentRun.lease_version == lease_version)
        res = await self.session.execute(
            update(AgentRun)
            .where(*conditions)
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    # ── 租约：原子 Claim / Heartbeat / Release（FR-02） ──────────────────

    async def claim(self, run_id: str, worker_id: str, lease_seconds: int) -> Optional[int]:
        """原子领取 Run。成功返回新 lease_version；不可领取返回 None。

        单条条件 UPDATE 即原子操作：pending，或 Lease 已过期/无主的 running。
        """
        now = datetime.utcnow()
        res = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_CLAIMABLE),
                or_(
                    AgentRun.worker_id.is_(None),
                    AgentRun.lease_expires_at.is_(None),
                    AgentRun.lease_expires_at < now,
                ),
            )
            .values(
                status=STATUS_RUNNING,
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                lease_version=AgentRun.lease_version + 1,
                attempt=AgentRun.attempt + 1,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount != 1:
            return None
        # started_at 首次置位（该 UPDATE 不区分首次，单独补一次）
        await self.session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.started_at.is_(None))
            .values(started_at=now),
            execution_options={"synchronize_session": False},
        )
        row = await self.session.execute(
            select(AgentRun.lease_version).where(AgentRun.id == run_id)
        )
        return row.scalar_one()

    async def claim_next(self, worker_id: str, lease_seconds: int) -> Optional[AgentRun]:
        """扫描可领取 Run 并原子领取（pending 优先，其次 Lease 过期的 running）。

        waiting_approval / needs_reconciliation 不会被领取（FR-02.9）。
        """
        now = datetime.utcnow()
        res = await self.session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.status.in_(_CLAIMABLE),
                or_(
                    AgentRun.worker_id.is_(None),
                    AgentRun.lease_expires_at.is_(None),
                    AgentRun.lease_expires_at < now,
                ),
            )
            .order_by(
                case((AgentRun.status == STATUS_PENDING, 0), else_=1),
                AgentRun.created_at,
            )
            .limit(10)
        )
        candidate_ids: List[str] = list(res.scalars().all())
        for run_id in candidate_ids:
            if await self.claim(run_id, worker_id, lease_seconds) is not None:
                return await self.get_run(run_id)
        return None

    async def claim_paused(
        self, run_id: str, worker_id: str, lease_seconds: int
    ) -> Optional[int]:
        """人工恢复路径的原子领取：暂停态（waiting_approval / needs_reconciliation）→ running。

        与 ``claim`` 一致地递增 attempt / lease_version，获得独立 Lease 与 fencing token。
        """
        now = datetime.utcnow()
        res = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(tuple(PAUSED_STATUSES)),
            )
            .values(
                status=STATUS_RUNNING,
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                lease_version=AgentRun.lease_version + 1,
                attempt=AgentRun.attempt + 1,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount != 1:
            return None
        row = await self.session.execute(
            select(AgentRun.lease_version).where(AgentRun.id == run_id)
        )
        return row.scalar_one()

    async def heartbeat(
        self, run_id: str, worker_id: str, lease_version: int, lease_seconds: int
    ) -> bool:
        """续租。返回 False 表示已丢失 Lease（fencing 失败），必须停止推进。"""
        now = datetime.utcnow()
        res = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.worker_id == worker_id,
                AgentRun.lease_version == lease_version,
                AgentRun.status == STATUS_RUNNING,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    async def release(self, run_id: str, worker_id: str, lease_version: int) -> bool:
        """释放 Lease 并递增 fencing token（使旧 Worker 永久失效）。"""
        res = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.worker_id == worker_id,
                AgentRun.lease_version == lease_version,
            )
            .values(
                worker_id=None,
                lease_expires_at=None,
                lease_version=AgentRun.lease_version + 1,
                updated_at=datetime.utcnow(),
            ),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    # ── 事件序号（FR-07：并发安全，不依赖 Worker 本地计数器） ────────────

    async def next_event_seq(self, run_id: str) -> int:
        """原子自增并返回 Run 内事件序号。"""
        res = await self.session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                last_event_seq=AgentRun.last_event_seq + 1,
                updated_at=datetime.utcnow(),
            ),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount != 1:
            raise LookupError(f"run {run_id} 不存在，无法分配事件序号")
        row = await self.session.execute(
            select(AgentRun.last_event_seq).where(AgentRun.id == run_id)
        )
        return row.scalar_one()

    # ── 过期/恢复扫描（FR-03） ─────────────────────────────────────────

    async def list_stale_running(self, now: Optional[datetime] = None) -> List[AgentRun]:
        """Lease 已过期的 running Run（进入恢复检查）。"""
        now = now or datetime.utcnow()
        res = await self.session.execute(
            select(AgentRun)
            .where(
                AgentRun.status == STATUS_RUNNING,
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at < now,
            )
            .execution_options(populate_existing=True)
        )
        return list(res.scalars().all())

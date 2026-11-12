"""Run 仓库：agent_runs 表的唯一读写入口。

设计要点（任务书 FR-01 / FR-02 / P0-02）：
- 创建与幂等由数据库唯一约束 ``uq_agent_runs_idem`` 保证，不依赖内存 Registry；
- Worker 执行权由原子条件 UPDATE（CAS）获得，同一 Run 同时只有一个有效 Lease；
- ``lease_version`` 作为 fencing token：每次 Claim/Release 递增，旧 Worker
  的状态提交因版本不匹配被拒绝；
- **P0-02 全条件 Fencing**：状态提交必须携带执行权身份（RunExecutionLease），
  WHERE 同时校验 Run ID / Worker ID / Lease Version / Lease 未过期 /
  当前状态允许迁移（数据库级状态机，终态不可覆盖）；失败按冲突类型抛出
  ``RunLeaseLost``（易主/过期）或 ``IllegalTransition``（终态覆盖/非法迁移）；
- 无 Lease 的取消（pending 用户取消）走专用 CAS，不覆盖正在执行的 Run；
- 租约时间比较统一使用数据库时间（``_sql_now`` / ``_sql_future``），
  避免不同机器时钟偏差导致租约判断不一致；
- 所有持久化失败向上抛出（IntegrityError/DBAPIError 不吞）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun
from ..lifecycle import (
    ALLOWED_TRANSITIONS,
    PAUSED_STATUSES,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    RunExecutionLease,
)
from .errors import IdempotencyConflict, IllegalTransition, RunLeaseLost

# claim_next 可领取的状态：pending，或 Lease 过期的 running（进入恢复检查）
_CLAIMABLE = (STATUS_PENDING, STATUS_RUNNING)

# ── heartbeat() 三态语义（任务书 P0-02） ─────────────────────────────────
# 心跳失败必须区分"真租约丢失"与"Run 已终态/暂停"：后两者是执行链路的
# 正常收尾或等待人工，若一律按租约丢失处理，会取消正在收尾的执行、
# 吞掉终止事件并悬挂所有事件流消费者。
HEARTBEAT_OK = "ok"              # 续租成功
HEARTBEAT_FINISHED = "finished"  # Run 已终态：心跳自然结束
HEARTBEAT_PAUSED = "paused"      # Run 已暂停（waiting_approval / needs_reconciliation）
HEARTBEAT_LOST = "lost"          # 真租约丢失：必须停止推进（fencing 失败）


# ── 数据库时间（P0-02：租约判断统一走 DB 时间，避免机器时钟偏差） ────────

def _dialect(session: AsyncSession) -> str:
    return session.sync_session.get_bind().dialect.name


def _sql_now(session: AsyncSession):
    """数据库当前时间（UTC）。与 Python 侧 utcnow 写入的时间戳同基准。"""
    d = _dialect(session)
    if d == "sqlite":
        return func.now()  # CURRENT_TIMESTAMP（UTC）
    if d in ("mysql", "mariadb"):
        return func.utc_timestamp()
    return func.timezone("UTC", func.now())  # postgresql


def _sql_future(session: AsyncSession, seconds: int):
    """数据库当前时间 + 秒数（用于写入 lease_expires_at）。"""
    d = _dialect(session)
    if d == "sqlite":
        return func.datetime("now", f"+{int(seconds)} seconds")
    if d in ("mysql", "mariadb"):
        return func.date_add(func.utc_timestamp(), text(f"INTERVAL {int(seconds)} SECOND"))
    return func.timezone("UTC", func.now()) + text(f"INTERVAL '{int(seconds)} seconds'")


def _allowed_sources(target_status: str) -> Tuple[str, ...]:
    """数据库级状态机：允许迁移到 target_status 的源状态集合。"""
    return tuple(
        src for src, dsts in ALLOWED_TRANSITIONS.items() if target_status in dsts
    )


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

    # ── 状态更新（P0-02：全条件 Fencing + 数据库级状态机） ──────────────

    async def update_status(
        self,
        run_id: str,
        *,
        status: str,
        lease: RunExecutionLease,
        expected_status: Optional[str] = None,
        error: Optional[str] = None,
        final_text: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        recovery_error: Optional[str] = None,
        finished: bool = False,
    ) -> bool:
        """提交 Run 状态（必须携带执行权身份）。

        WHERE 同时校验（缺一不可）：
        - Run ID / Worker ID / Lease Version（Fencing token）；
        - Lease 未过期（数据库时间比较）；
        - 当前状态允许迁移到 ``status``（数据库级状态机：终态不可覆盖、
          非法迁移被拒）；
        - ``expected_status`` 命中（提供时）。

        命中失败按冲突类型抛出：
        - ``RunLeaseLost``：租约易主 / 已过期 / 并发竞争 —— 调用方必须
          立即停止推进（不写 Session、不发布终态事件、不产生新副作用）；
        - ``IllegalTransition``：终态覆盖 / expected_status 不匹配 / 状态机
          拒绝 —— 调用方不得继续写库。
        """
        if lease is None:
            raise RunLeaseLost(f"run {run_id} 状态提交缺少执行权身份（lease 为 None）")

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

        conditions = [
            AgentRun.id == run_id,
            AgentRun.worker_id == lease.worker_id,
            AgentRun.lease_version == lease.lease_version,
            AgentRun.lease_expires_at.isnot(None),
            AgentRun.lease_expires_at > _sql_now(self.session),
            AgentRun.status.in_(_allowed_sources(status)),
        ]
        if expected_status is not None:
            conditions.append(AgentRun.status == expected_status)

        res = await self.session.execute(
            update(AgentRun)
            .where(*conditions)
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount == 1:
            return True
        await self._raise_status_conflict(run_id, lease, status, expected_status)
        return False  # 不可达：_raise_status_conflict 必定抛出

    async def _raise_status_conflict(
        self,
        run_id: str,
        lease: RunExecutionLease,
        status: str,
        expected_status: Optional[str],
    ) -> None:
        """分类状态提交冲突，抛出明确异常（RunLeaseLost / IllegalTransition）。"""
        row = await self.get_run(run_id)
        if row is None:
            raise IllegalTransition(f"run {run_id} 不存在，状态提交被拒")
        if row.worker_id != lease.worker_id or row.lease_version != lease.lease_version:
            raise RunLeaseLost(
                f"run {run_id} 执行权已易主（当前 {row.worker_id}#v{row.lease_version}，"
                f"提交方 {lease.worker_id}#v{lease.lease_version}），状态提交被拒"
            )
        if row.lease_expires_at is None or row.lease_expires_at <= datetime.utcnow():
            # LF-02：租约过期后旧 Worker 不得再提交状态（即使无人接管）
            raise RunLeaseLost(
                f"run {run_id} 租约已过期，状态提交被拒（须重新 Claim）"
            )
        if row.status in TERMINAL_STATUSES:
            # LF-05：终态不可覆盖（completed 不得降级为 failed 等）
            raise IllegalTransition(
                f"run {run_id} 已处于终态 {row.status}，不得覆盖为 {status}"
            )
        if expected_status is not None and row.status != expected_status:
            raise IllegalTransition(
                f"run {run_id} 期望状态 {expected_status}，实际 {row.status}"
            )
        if status not in ALLOWED_TRANSITIONS.get(row.status, set()):
            raise IllegalTransition(
                f"run {run_id} 状态迁移 {row.status} -> {status} 不允许"
            )
        # 所有身份/状态条件均满足但 CAS 未命中：并发竞争，按执行权丢失处理
        raise RunLeaseLost(
            f"run {run_id} 状态提交未命中（并发竞争），视为执行权丢失"
        )

    async def record_recovery(
        self,
        run_id: str,
        lease: RunExecutionLease,
        *,
        metadata: Dict[str, Any],
        recovery_error: Optional[str] = None,
    ) -> bool:
        """记录恢复决策元数据（P0-04 §6.3：所有恢复决策写入 Run recovery metadata）。

        与状态提交相同的 Fencing 条件（Worker ID / Lease Version / Lease 未过期），
        但不改变 Run 状态；冲突时抛 ``RunLeaseLost`` / ``IllegalTransition``。
        """
        if lease is None:
            raise RunLeaseLost(f"run {run_id} 恢复元数据写入缺少执行权身份（lease 为 None）")
        values: Dict[str, Any] = {
            "recovery_metadata": metadata,
            "updated_at": datetime.utcnow(),
        }
        if recovery_error is not None:
            values["recovery_error"] = recovery_error
        res = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.worker_id == lease.worker_id,
                AgentRun.lease_version == lease.lease_version,
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at > _sql_now(self.session),
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount == 1:
            return True
        await self._raise_status_conflict(run_id, lease, "(recovery_metadata)", None)
        return False  # 不可达：_raise_status_conflict 必定抛出

    async def cancel_without_lease(
        self,
        run_id: str,
        *,
        from_statuses: Optional[Tuple[str, ...]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """无 Lease 取消专用 CAS（任务书 P0-02 C / LF-06）。

        只允许取消 pending / 暂停态（waiting_approval / needs_reconciliation）
        的 Run；正在执行的 Run 必须走挂起 + Fencing 路径，不得被无条件覆盖。
        与 Worker Claim 并发时只有一个操作成功。
        """
        allowed = tuple(from_statuses) if from_statuses is not None else (STATUS_PENDING,)
        values: Dict[str, Any] = {
            "status": STATUS_CANCELLED,
            "finished_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if error is not None:
            values["error"] = error
        res = await self.session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status.in_(allowed))
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    # ── 租约：原子 Claim / Heartbeat / Release（FR-02 / P0-02 B） ───────

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
                    AgentRun.lease_expires_at < _sql_now(self.session),
                ),
            )
            .values(
                status=STATUS_RUNNING,
                worker_id=worker_id,
                lease_expires_at=_sql_future(self.session, lease_seconds),
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
        res = await self.session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.status.in_(_CLAIMABLE),
                or_(
                    AgentRun.worker_id.is_(None),
                    AgentRun.lease_expires_at.is_(None),
                    AgentRun.lease_expires_at < _sql_now(self.session),
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
                lease_expires_at=_sql_future(self.session, lease_seconds),
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
    ) -> str:
        """续租。返回 HEARTBEAT_* 三态语义（任务书 P0-02）：

        - ``HEARTBEAT_OK``：续租成功；
        - ``HEARTBEAT_FINISHED``：Run 已终态——心跳自然结束，**不是**租约丢失，
          不得据此取消正在收尾的执行（否则会吞掉终止事件、悬挂消费者）；
        - ``HEARTBEAT_PAUSED``：Run 已进入暂停态（waiting_approval /
          needs_reconciliation）——执行自行挂起等待人工，同样不是租约丢失；
        - ``HEARTBEAT_LOST``：真租约丢失（worker_id / lease_version 易主，
          或租约已过期），必须停止推进（fencing 失败，宁可恢复、不可重复副作用）。

        严格续租（LF-01）：租约已过期时即使尚未被新 Worker Claim，
        原 Worker 也不得自行恢复租约，必须重新经过 Claim。
        """
        now = datetime.utcnow()
        res = await self.session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.worker_id == worker_id,
                AgentRun.lease_version == lease_version,
                AgentRun.status == STATUS_RUNNING,
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at > _sql_now(self.session),
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=_sql_future(self.session, lease_seconds),
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount == 1:
            return HEARTBEAT_OK
        row = await self.session.execute(
            select(
                AgentRun.status,
                AgentRun.worker_id,
                AgentRun.lease_version,
                AgentRun.lease_expires_at,
            ).where(AgentRun.id == run_id)
        )
        rec = row.first()
        if rec is None:
            return HEARTBEAT_LOST
        status, cur_worker, cur_version, _expires_at = rec
        # 终态/暂停优先：执行结果已决定，不得误判为租约丢失
        if status in TERMINAL_STATUSES:
            return HEARTBEAT_FINISHED
        if status in PAUSED_STATUSES:
            return HEARTBEAT_PAUSED
        if cur_worker != worker_id or cur_version != lease_version:
            return HEARTBEAT_LOST
        # LF-01：同 worker/版本但续租失败 = 租约已过期，不得自行恢复
        return HEARTBEAT_LOST

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
        if now is None:
            cond = AgentRun.lease_expires_at < _sql_now(self.session)
        else:
            cond = AgentRun.lease_expires_at < now
        res = await self.session.execute(
            select(AgentRun)
            .where(
                AgentRun.status == STATUS_RUNNING,
                AgentRun.lease_expires_at.isnot(None),
                cond,
            )
            .execution_options(populate_existing=True)
        )
        return list(res.scalars().all())

"""Tool Execution Ledger 仓库：agent_tool_executions 表。

核心语义（任务书 FR-05 / P0-03 §5）：

- 副作用先登记（prepared），再执行（executing），再确认（succeeded/failed/unknown）；
- ``(run_id, tool_call_id)`` 是稳定唯一身份 —— 禁止用 args_hash 兼当调用 ID；
- **调用身份一致性（§5.2 B）**：同身份再次登记时必须比对
  tool_name / args_hash / idempotency_key（含 None 一致性），任何不一致抛
  ``ToolCallIdentityConflict`` —— 不返回旧缓存、不继续执行；
- **执行权无条件校验（§5.2 A）**：全部写入（begin/mark_executing/终态确认/人工核对）
  必须携带 ``RunExecutionLease``，且执行权 SELECT 校验与条件 UPDATE
  在同一事务内（校验 SELECT 带 ``FOR UPDATE`` 与 claim 的 agent_runs
  更新互斥，消除 TOCTOU）。SQLite 不支持行级锁，由单写者串行化保证
  等价隔离 —— 能力边界如实标注，不能以 SQLite 通过代替生产数据库通过；
- **严格状态机（§5.2 C，唯一事实来源 ``_ALLOWED_TRANSITIONS``）**::

    prepared ──> executing ──> {succeeded | failed | unknown}
    unknown  ──> needs_reconciliation（人工核对；Batch 6 前普通路径不可迁移）

  - ``succeeded`` 不可覆盖（任何方向）；
  - ``needs_reconciliation`` 不可被普通重试覆盖；
  - ``executing/failed/unknown`` 重新进入 ``executing`` 必须 ``reentry=True``
    （副作用等级判定的唯一入口：由 adapter 的
    ``classification.replay_action == REPLAY_RETRY`` 提供）；
  - ``prepared -> executing`` 是一次性原子翻转（WHERE status='prepared'），
    并发执行同一 tool_call_id 时最多一个竞争者胜出；
  - 任何迁移失败都以 ``IllegalTransition`` 显式抛出，必须被 Adapter 感知。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun, AgentToolExecution
from ..lifecycle import STATUS_RUNNING, RunExecutionLease
from .errors import IllegalTransition, RunLeaseLost, ToolCallIdentityConflict
from .run_repository import _sql_now

TOOL_EXEC_PREPARED = "prepared"
TOOL_EXEC_EXECUTING = "executing"
TOOL_EXEC_SUCCEEDED = "succeeded"
TOOL_EXEC_FAILED = "failed"
TOOL_EXEC_UNKNOWN = "unknown"
TOOL_EXEC_NEEDS_RECONCILIATION = "needs_reconciliation"

# 恢复时不允许自动重放的状态
UNSAFE_TO_REPLAY = {TOOL_EXEC_EXECUTING, TOOL_EXEC_UNKNOWN, TOOL_EXEC_NEEDS_RECONCILIATION}

# 唯一事实来源：合法状态迁移表（§5.2 C）
_ALLOWED_TRANSITIONS: Dict[str, set] = {
    TOOL_EXEC_PREPARED: {TOOL_EXEC_EXECUTING},
    TOOL_EXEC_EXECUTING: {
        TOOL_EXEC_EXECUTING,
        TOOL_EXEC_SUCCEEDED,
        TOOL_EXEC_FAILED,
        TOOL_EXEC_UNKNOWN,
        TOOL_EXEC_NEEDS_RECONCILIATION,
    },
    TOOL_EXEC_FAILED: {TOOL_EXEC_EXECUTING, TOOL_EXEC_NEEDS_RECONCILIATION},
    TOOL_EXEC_UNKNOWN: {TOOL_EXEC_EXECUTING, TOOL_EXEC_NEEDS_RECONCILIATION},
    TOOL_EXEC_SUCCEEDED: set(),  # 终态：不可覆盖
    TOOL_EXEC_NEEDS_RECONCILIATION: set(),  # 人工核对前不可迁移
}

# mark_executing 允许的源状态：
# - 非 reentry：仅 prepared —— prepared->executing 是一次性原子翻转，
#   并发同一 tool_call_id 最多一个有效执行者（§5.3 第 7 条）；
# - reentry：executing/failed/unknown 残留恢复（必须经副作用等级判定）。
_EXEC_SOURCES: Dict[bool, Tuple[str, ...]] = {
    False: (TOOL_EXEC_PREPARED,),
    True: (TOOL_EXEC_FAILED, TOOL_EXEC_EXECUTING, TOOL_EXEC_UNKNOWN),
}

# 终态确认允许的源状态（均只允许从 executing 确认执行结果；
# needs_reconciliation 仅允许从未决/不确定状态转入）
_FINISH_SOURCES: Dict[str, Tuple[str, ...]] = {
    TOOL_EXEC_SUCCEEDED: (TOOL_EXEC_EXECUTING,),
    TOOL_EXEC_FAILED: (TOOL_EXEC_EXECUTING,),
    TOOL_EXEC_UNKNOWN: (TOOL_EXEC_EXECUTING,),
    TOOL_EXEC_NEEDS_RECONCILIATION: (
        TOOL_EXEC_EXECUTING,
        TOOL_EXEC_FAILED,
        TOOL_EXEC_UNKNOWN,
    ),
}


class ToolExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def begin(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        args_hash: Optional[str] = None,
        args_snapshot: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        lease: RunExecutionLease,
    ) -> Tuple[AgentToolExecution, bool]:
        """登记执行意图（prepared）。返回 (record, created)。

        已存在同 (run_id, tool_call_id) 记录（崩溃恢复/重复投递）时，
        必须比对 tool_name/args_hash/idempotency_key（含 None 一致性），
        任何不一致抛 ``ToolCallIdentityConflict``：不返回旧缓存、不继续执行。
        """
        await self._verify_lease(lease)
        existing = await self.get(run_id, tool_call_id)
        if existing is not None:
            self._check_identity(
                existing,
                tool_name=tool_name,
                args_hash=args_hash,
                idempotency_key=idempotency_key,
            )
            return existing, False

        rec = AgentToolExecution(
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_hash=args_hash,
            args_snapshot=args_snapshot,
            idempotency_key=idempotency_key,
            status=TOOL_EXEC_PREPARED,
        )
        self.session.add(rec)
        try:
            await self.session.flush()
        except IntegrityError:
            # 并发登记竞态：回滚后重查已有记录，走同一身份比较
            await self.session.rollback()
            existing = await self.get(run_id, tool_call_id)
            if existing is None:
                raise
            self._check_identity(
                existing,
                tool_name=tool_name,
                args_hash=args_hash,
                idempotency_key=idempotency_key,
            )
            return existing, False
        return rec, True

    @staticmethod
    def _check_identity(
        existing: AgentToolExecution,
        *,
        tool_name: str,
        args_hash: Optional[str],
        idempotency_key: Optional[str],
    ) -> None:
        """调用身份一致性比较（含 None 一致性），任何不一致显式冲突（§5.2 B）。"""
        conflicts = []
        if existing.tool_name != tool_name:
            conflicts.append(
                f"tool_name 不一致: 已存 {existing.tool_name!r} != 本次 {tool_name!r}"
            )
        if existing.args_hash != args_hash:
            conflicts.append(
                f"args_hash 不一致: 已存 {existing.args_hash!r} != 本次 {args_hash!r}"
            )
        if existing.idempotency_key != idempotency_key:
            conflicts.append(
                f"idempotency_key 不一致: 已存 {existing.idempotency_key!r}"
                f" != 本次 {idempotency_key!r}"
            )
        if conflicts:
            raise ToolCallIdentityConflict(
                f"调用身份冲突 run={existing.run_id} tool_call_id={existing.tool_call_id}: "
                + "；".join(conflicts)
                + "。同 tool_call_id 不得携带不同调用内容，拒绝返回缓存、拒绝继续执行"
            )

    async def get(self, run_id: str, tool_call_id: str) -> Optional[AgentToolExecution]:
        res = await self.session.execute(
            select(AgentToolExecution)
            .where(
                AgentToolExecution.run_id == run_id,
                AgentToolExecution.tool_call_id == tool_call_id,
            )
            .execution_options(populate_existing=True)
        )
        return res.scalar_one_or_none()

    async def _verify_lease(self, lease: RunExecutionLease) -> None:
        """LF-07 / §5.2 A：Ledger 写入前校验执行权（与条件更新同一事务，数据库时间比较）。

        校验 Run ID / Worker ID / Lease Version / Run 处于 running /
        Lease 未过期；任一不满足抛 ``RunLeaseLost``，旧 Worker 的
        Tool Ledger 写入一律被拒绝。

        SELECT 带 ``FOR UPDATE`` 与 claim 的 agent_runs UPDATE 互斥，
        避免"校验后、更新前"被接管的 TOCTOU。SQLite 不支持行级锁
        （SQLAlchemy SQLite 方言忽略 FOR UPDATE），由单写者串行化提供
        等价隔离；MySQL/PostgreSQL 生产库走真实行锁。
        """
        res = await self.session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.id == lease.run_id,
                AgentRun.worker_id == lease.worker_id,
                AgentRun.lease_version == lease.lease_version,
                AgentRun.status == STATUS_RUNNING,
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at > _sql_now(self.session),
            )
            .with_for_update()
        )
        if res.first() is None:
            raise RunLeaseLost(
                f"run {lease.run_id} 执行权校验失败"
                f"（worker={lease.worker_id} lease_version={lease.lease_version}），"
                "拒绝 Tool Ledger 写入"
            )

    async def mark_executing(
        self,
        exec_id: int,
        *,
        lease: RunExecutionLease,
        reentry: bool = False,
    ) -> bool:
        """进入执行态。返回是否成功获得本次执行资格。

        - ``reentry=False``：仅允许 ``prepared -> executing``，一次性原子翻转；
          并发调用同一 tool_call_id 时最多一个返回 True（有效执行者唯一）。
        - ``reentry=True``：``executing/failed/unknown`` 残留恢复重入。唯一
          合法来源是 adapter 的副作用等级判定
          （``classification.replay_action == REPLAY_RETRY``）；调用方不得
          在未经副作用等级判定时使用。

        ``succeeded`` / ``needs_reconciliation`` 任何路径不可进入 executing。
        竞态/非法迁移返回 False（由上层优雅拒绝或重查处理）。
        """
        await self._verify_lease(lease)
        res = await self.session.execute(
            update(AgentToolExecution)
            .where(
                AgentToolExecution.id == exec_id,
                AgentToolExecution.status.in_(_EXEC_SOURCES[bool(reentry)]),
            )
            .values(
                status=TOOL_EXEC_EXECUTING,
                attempt_count=AgentToolExecution.attempt_count + 1,
                worker_id=lease.worker_id,
                lease_version=lease.lease_version,
                started_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    async def mark_succeeded(
        self,
        exec_id: int,
        *,
        result_payload: Optional[Any] = None,
        external_operation_id: Optional[str] = None,
        lease: RunExecutionLease,
    ) -> bool:
        return await self._finish(
            exec_id, TOOL_EXEC_SUCCEEDED,
            result_payload=result_payload,
            external_operation_id=external_operation_id,
            lease=lease,
        )

    async def mark_failed(
        self, exec_id: int, *, error: Optional[str] = None,
        lease: RunExecutionLease,
    ) -> bool:
        return await self._finish(exec_id, TOOL_EXEC_FAILED, error=error, lease=lease)

    async def mark_unknown(
        self, exec_id: int, *, error: Optional[str] = None,
        lease: RunExecutionLease,
    ) -> bool:
        """结果无法确认（F-07/F-08）：禁止自动重放，等待核对。"""
        return await self._finish(exec_id, TOOL_EXEC_UNKNOWN, error=error, lease=lease)

    async def mark_needs_reconciliation(
        self, exec_id: int, *, error: Optional[str] = None,
        lease: RunExecutionLease,
    ) -> bool:
        return await self._finish(
            exec_id, TOOL_EXEC_NEEDS_RECONCILIATION, error=error, lease=lease
        )

    async def reconcile(
        self,
        exec_id: int,
        *,
        disposition: str,
        external_operation_id: Optional[str] = None,
        note: Optional[str] = None,
        reconciled_by: Optional[int] = None,
    ) -> bool:
        """人工核对专用迁移口（P1-03 §11 D）。

        ``needs_reconciliation`` 在 ``_ALLOWED_TRANSITIONS`` 中是死终态（空集），
        普通路径不可迁移；人工核对必须走此专用口做条件状态迁移
        （``WHERE status='needs_reconciliation'``）。三处置：
        - ``confirmed_succeeded``：外部副作用确认已发生 -> ``succeeded``
          （记录 ``external_operation_id``，去重复用）；
        - ``confirmed_not_executed``：确认未执行 -> ``prepared``（允许安全恢复/重试）；
        - ``unresolved``：维持 ``needs_reconciliation``，仅留痕审计。

        返回是否命中（仅从 needs_reconciliation 出发的条件更新成功）。调用方不得
        在此触发任何重新推理。
        """
        if disposition not in (
            "confirmed_succeeded", "confirmed_not_executed", "unresolved"
        ):
            raise ValueError(f"未知核对处置: {disposition}")
        values: Dict[str, Any] = {
            "reconcile_decision": disposition,
            "reconcile_note": note,
            "reconciled_by": reconciled_by,
            "reconciled_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if external_operation_id is not None:
            values["external_operation_id"] = external_operation_id
        if disposition == "confirmed_succeeded":
            values["status"] = TOOL_EXEC_SUCCEEDED
        elif disposition == "confirmed_not_executed":
            values["status"] = TOOL_EXEC_PREPARED
        # unresolved：不改 status，仅审计留痕
        res = await self.session.execute(
            update(AgentToolExecution)
            .where(
                AgentToolExecution.id == exec_id,
                AgentToolExecution.status == TOOL_EXEC_NEEDS_RECONCILIATION,
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

    async def _finish(
        self,
        exec_id: int,
        status: str,
        *,
        result_payload: Optional[Any] = None,
        error: Optional[str] = None,
        external_operation_id: Optional[str] = None,
        lease: RunExecutionLease,
    ) -> bool:
        """终态确认（严格状态机）。

        仅允许从 ``_FINISH_SOURCES`` 规定的源状态迁移；``succeeded`` 不可被
        失败覆盖，``needs_reconciliation`` 不可被普通路径覆盖。迁移失败抛
        ``IllegalTransition``（§5.2 D：状态冲突必须显式、必须被 Adapter 感知）。
        """
        await self._verify_lease(lease)
        values: Dict[str, Any] = {
            "status": status,
            "finished_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if result_payload is not None:
            values["result_payload"] = result_payload
        if error is not None:
            values["error"] = error
        if external_operation_id is not None:
            values["external_operation_id"] = external_operation_id
        res = await self.session.execute(
            update(AgentToolExecution)
            .where(
                AgentToolExecution.id == exec_id,
                AgentToolExecution.status.in_(_FINISH_SOURCES[status]),
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        if res.rowcount != 1:
            cur = await self.session.execute(
                select(AgentToolExecution.status).where(AgentToolExecution.id == exec_id)
            )
            current = cur.scalar_one_or_none()
            raise IllegalTransition(
                f"Tool Ledger 状态迁移被拒: {current} -> {status}"
                f"（exec_id={exec_id}）。终态/人工核对态不可被覆盖"
            )
        return True

    async def list_unresolved(self, run_id: str) -> List[AgentToolExecution]:
        """恢复检查用：该 Run 中未明确终结的执行记录。"""
        res = await self.session.execute(
            select(AgentToolExecution)
            .where(
                AgentToolExecution.run_id == run_id,
                AgentToolExecution.status.notin_(
                    (TOOL_EXEC_SUCCEEDED, TOOL_EXEC_FAILED)
                ),
            )
            .execution_options(populate_existing=True)
        )
        return list(res.scalars().all())

    async def has_unsafe_entries(self, run_id: str) -> bool:
        """是否存在结果未知、禁止自动重放的执行（决定是否 needs_reconciliation）。"""
        res = await self.session.execute(
            select(AgentToolExecution.id)
            .where(
                AgentToolExecution.run_id == run_id,
                AgentToolExecution.status.in_(tuple(UNSAFE_TO_REPLAY)),
            )
            .limit(1)
        )
        return res.first() is not None

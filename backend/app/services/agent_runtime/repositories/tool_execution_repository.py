"""Tool Execution Ledger 仓库：agent_tool_executions 表。

核心语义（任务书 FR-05）：
- 副作用先登记（prepared），再执行（executing），再确认（succeeded/failed/unknown）；
- ``(run_id, tool_call_id)`` 是稳定唯一身份 —— 禁止用 args_hash 兼当调用 ID；
- 已 succeeded 的调用返回已存结果，不重复执行；
- 结果未知（unknown / executing 残留）的调用禁止自动重放，进入 needs_reconciliation。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun, AgentToolExecution
from ..lifecycle import STATUS_RUNNING, RunExecutionLease
from .errors import RunLeaseLost
from .run_repository import _sql_now

TOOL_EXEC_PREPARED = "prepared"
TOOL_EXEC_EXECUTING = "executing"
TOOL_EXEC_SUCCEEDED = "succeeded"
TOOL_EXEC_FAILED = "failed"
TOOL_EXEC_UNKNOWN = "unknown"
TOOL_EXEC_NEEDS_RECONCILIATION = "needs_reconciliation"

# 恢复时不允许自动重放的状态
UNSAFE_TO_REPLAY = {TOOL_EXEC_EXECUTING, TOOL_EXEC_UNKNOWN, TOOL_EXEC_NEEDS_RECONCILIATION}


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
    ) -> Tuple[AgentToolExecution, bool]:
        """登记执行意图（prepared）。返回 (record, created)。

        已存在同 (run_id, tool_call_id) 记录时复用（崩溃恢复/重复投递）。
        """
        existing = await self.get(run_id, tool_call_id)
        if existing is not None:
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
            await self.session.rollback()
            existing = await self.get(run_id, tool_call_id)
            if existing is None:
                raise
            return existing, False
        return rec, True

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
        """LF-07：Ledger 写入前校验执行权（同事务内，数据库时间比较）。

        校验 Run ID / Worker ID / Lease Version / Run 处于 running /
        Lease 未过期；任一不满足抛 ``RunLeaseLost``，旧 Worker 的
        Tool Ledger 写入一律被拒绝。
        """
        res = await self.session.execute(
            select(AgentRun.id).where(
                AgentRun.id == lease.run_id,
                AgentRun.worker_id == lease.worker_id,
                AgentRun.lease_version == lease.lease_version,
                AgentRun.status == STATUS_RUNNING,
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at > _sql_now(self.session),
            )
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
        worker_id: Optional[str] = None,
        lease_version: int = 0,
        lease: Optional[RunExecutionLease] = None,
    ) -> bool:
        if lease is not None:
            await self._verify_lease(lease)
        res = await self.session.execute(
            update(AgentToolExecution)
            .where(
                AgentToolExecution.id == exec_id,
                AgentToolExecution.status.in_(
                    # executing/unknown 残留是否可重试由上层按副作用等级判定
                    # （tools/classification.replay_action）：read_only 与
                    # external_idempotent（复用同一幂等键）允许重试，其余禁止。
                    (
                        TOOL_EXEC_PREPARED,
                        TOOL_EXEC_FAILED,
                        TOOL_EXEC_EXECUTING,
                        TOOL_EXEC_UNKNOWN,
                    )
                ),
            )
            .values(
                status=TOOL_EXEC_EXECUTING,
                attempt_count=AgentToolExecution.attempt_count + 1,
                worker_id=worker_id,
                lease_version=lease_version,
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
        lease: Optional[RunExecutionLease] = None,
    ) -> bool:
        return await self._finish(
            exec_id, TOOL_EXEC_SUCCEEDED,
            result_payload=result_payload,
            external_operation_id=external_operation_id,
            lease=lease,
        )

    async def mark_failed(
        self, exec_id: int, *, error: Optional[str] = None,
        lease: Optional[RunExecutionLease] = None,
    ) -> bool:
        return await self._finish(exec_id, TOOL_EXEC_FAILED, error=error, lease=lease)

    async def mark_unknown(
        self, exec_id: int, *, error: Optional[str] = None,
        lease: Optional[RunExecutionLease] = None,
    ) -> bool:
        """结果无法确认（F-07/F-08）：禁止自动重放，等待核对。"""
        return await self._finish(exec_id, TOOL_EXEC_UNKNOWN, error=error, lease=lease)

    async def mark_needs_reconciliation(
        self, exec_id: int, *, error: Optional[str] = None,
        lease: Optional[RunExecutionLease] = None,
    ) -> bool:
        return await self._finish(
            exec_id, TOOL_EXEC_NEEDS_RECONCILIATION, error=error, lease=lease
        )

    async def _finish(
        self,
        exec_id: int,
        status: str,
        *,
        result_payload: Optional[Any] = None,
        error: Optional[str] = None,
        external_operation_id: Optional[str] = None,
        lease: Optional[RunExecutionLease] = None,
    ) -> bool:
        if lease is not None:
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
                AgentToolExecution.status.notin_(
                    (TOOL_EXEC_SUCCEEDED, TOOL_EXEC_NEEDS_RECONCILIATION)
                ),
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        return res.rowcount == 1

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

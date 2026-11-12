"""Run Worker：数据库租约协调的执行器（Claim / Lease / Heartbeat / Fencing）。

设计要点（任务书 FR-02 / FR-03 / F-01~F-05 / F-13）：
- 执行权由原子条件 UPDATE（CAS）获得：同一 Run 同时只有一个有效 Lease；
- 执行期间心跳续租；续租失败或无法确认时立即挂起执行（宁可恢复，不可重复副作用）；
- ``lease_version`` 为 fencing token：租约易主后旧 Worker 的一切状态提交被拒绝；
- 重启恢复：重新领取的 Run（attempt > 1）先做恢复检查，分为
  继续执行（Checkpoint 续跑）/ 回到待审批 / needs_reconciliation（禁止自动重放）；
- 优雅关闭：停止领取、等待在途 Run，超时后挂起（不写 cancelled，保留可恢复状态）；
- 状态写入失败一律向上抛出（PersistenceError），不允许吞掉后继续执行副作用。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from app.core.config import settings
from app.core.database import AsyncSessionLocal

from .events import (
    APPROVAL_REQUIRED,
    RUN_WAITING_APPROVAL,
    LegacyEventAdapter,
    RuntimeEvent,
    redact_sensitive,
)
from .lifecycle import (
    RECOVERY_CONTINUE,
    RECOVERY_FRESH,
    RECOVERY_NEEDS_RECONCILIATION,
    RECOVERY_WAITING_APPROVAL,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    RunHandle,
)
from .repositories import (
    EventRepository,
    HEARTBEAT_FINISHED,
    HEARTBEAT_LOST,
    HEARTBEAT_OK,
    HEARTBEAT_PAUSED,
    RunRepository,
)
from .repositories.errors import RunLeaseLost

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免循环导入
    from .service import AgentRuntimeService

logger = logging.getLogger("agent_runtime")

# 恢复检查结论常量已迁至 lifecycle（RECOVERY_*），此处保留导出兼容


@dataclass
class RunJob:
    """一次执行意图。

    进程内快速路径携带请求输入；跨进程/重启恢复路径由 agent_runs 的
    input_payload / agent_config_snapshot 重建。
    """

    run_id: str
    user_id: Optional[int] = None
    messages: Optional[List[Dict[str, Any]]] = None
    resume: Optional[Dict[str, Any]] = None
    agent_config: Optional[Dict[str, Any]] = None
    use_memory_context: str = ""
    db_info: Optional[Dict[str, Any]] = None


def default_worker_id() -> str:
    if getattr(settings, "AGENT_RUNTIME_WORKER_ID", ""):
        return settings.AGENT_RUNTIME_WORKER_ID
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class RunWorker:
    """数据库租约协调的 Run 执行器（每个进程一个实例）。

    - ``claim_run()``：原子领取（pending 或 Lease 过期的 running；暂停态走人工恢复）；
    - ``run_claimed()``：恢复检查后驱动执行；
    - ``supervise()``：心跳续租 + fencing 保护 + 释放租约；
    - ``start()/stop()``：轮询孤儿 Run 与优雅关闭。
    """

    def __init__(
        self,
        service: "AgentRuntimeService",
        *,
        worker_id: Optional[str] = None,
        lease_seconds: Optional[int] = None,
        heartbeat_interval: Optional[float] = None,
        poll_interval: Optional[float] = None,
        shutdown_grace: Optional[float] = None,
    ) -> None:
        self.service = service
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = (
            lease_seconds if lease_seconds is not None
            else getattr(settings, "AGENT_RUNTIME_LEASE_SECONDS", 60)
        )
        self.heartbeat_interval = (
            heartbeat_interval if heartbeat_interval is not None
            else getattr(settings, "AGENT_RUNTIME_HEARTBEAT_INTERVAL", 15.0)
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None
            else getattr(settings, "AGENT_RUNTIME_POLL_INTERVAL", 1.0)
        )
        self.shutdown_grace = (
            shutdown_grace if shutdown_grace is not None
            else getattr(settings, "AGENT_RUNTIME_SHUTDOWN_GRACE", 30.0)
        )
        self._poll_task: Optional[asyncio.Task] = None
        self._inflight: Set[asyncio.Task] = set()
        self._active: Dict[str, RunHandle] = {}
        self._stopping = False
        # 事件保留清理（FR-07）：周期性执行，默认每小时一次
        self._event_cleanup_interval = max(
            60.0, float(getattr(settings, "AGENT_EVENT_CLEANUP_INTERVAL", 3600.0))
        )
        self._next_event_cleanup = time.monotonic() + self._event_cleanup_interval

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动扫描循环（领取 pending / Lease 过期的孤儿 Run）。"""
        if self._poll_task is not None:
            return
        self._stopping = False
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "RunWorker %s 启动（lease=%ss heartbeat=%.1fs poll=%.1fs）",
            self.worker_id, self.lease_seconds, self.heartbeat_interval, self.poll_interval,
        )

    async def stop(self, graceful: bool = True) -> None:
        """优雅关闭：停止领取，等待在途 Run 结束；超时则挂起（保留可恢复状态）。"""
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None

        tasks = set(self._inflight)
        if tasks and graceful:
            _, pending = await asyncio.wait(tasks, timeout=self.shutdown_grace)
            tasks = pending
        for handle in self._active.values():
            # 挂起语义：不写 cancelled，让其他 Worker 经 Checkpoint 恢复
            handle.suspend_requested = True
            handle.suspend_reason = "shutdown"
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("RunWorker %s 已停止", self.worker_id)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return task

    # ── 领取（CAS） ─────────────────────────────────────────────────────

    async def claim_run(self, run_id: str, *, paused: bool = False) -> Optional[Any]:
        """原子领取 Run，返回领取后的 agent_runs 行（含新 lease_version）；失败返回 None。"""
        try:
            async with AsyncSessionLocal() as db:
                repo = RunRepository(db)
                if paused:
                    lease_version = await repo.claim_paused(
                        run_id, self.worker_id, self.lease_seconds
                    )
                else:
                    lease_version = await repo.claim(
                        run_id, self.worker_id, self.lease_seconds
                    )
                row = await repo.get_run(run_id) if lease_version is not None else None
                await db.commit()
            return row
        except Exception:
            logger.exception("run %s 领取失败", run_id)
            return None

    async def _claim_and_execute(self, job: RunJob, *, paused: bool = False) -> None:
        row = await self.claim_run(job.run_id, paused=paused)
        if row is None:
            logger.info("run %s 未能领取（已被其他 Worker 持有或状态不允许）", job.run_id)
            return
        try:
            await self.run_claimed(row, job)
        except Exception:
            logger.exception("Worker 驱动 run %s 异常", job.run_id)

    # ── 执行与恢复 ──────────────────────────────────────────────────────

    async def run_claimed(self, row: Any, job: RunJob) -> None:
        """对已领取的 Run 做恢复检查并驱动执行（租约已获得）。"""
        handle = self.service.registry.attach(
            run_id=row.id,
            agent_id=row.agent_id,
            session_id=row.session_id,
            user_id=row.user_id,
            thread_id=row.thread_id,
            idempotency_key=row.idempotency_key,
            status=row.status,
        )
        handle.lease_version = row.lease_version
        handle.worker_id = self.worker_id
        handle.lease_expires_at = row.lease_expires_at
        handle.attempt = row.attempt or 0
        if handle.status == STATUS_PENDING:
            handle.transition(STATUS_RUNNING)

        # 显式恢复（HITL resume）跳过恢复检查：决策合法性由审批层负责（Phase 4）
        if job.resume is not None:
            mode, reason, actions = RECOVERY_FRESH, "", []
        else:
            mode, reason, actions, _recovery_meta = await self.service._recovery_check(row)

        if mode == RECOVERY_NEEDS_RECONCILIATION:
            await self._pause_run(handle, STATUS_NEEDS_RECONCILIATION, reason=reason)
            return
        if mode == RECOVERY_WAITING_APPROVAL:
            await self._pause_run(handle, STATUS_WAITING_APPROVAL, actions=actions)
            return

        if mode == RECOVERY_CONTINUE:
            job = replace(job, messages=None, resume=None)
        elif job.messages is None and job.resume is None:
            # 恢复路径：从数据库输入快照重建请求输入
            db_job = self._job_from_row(row)
            job = replace(
                job,
                messages=db_job.messages,
                agent_config=job.agent_config or db_job.agent_config,
                use_memory_context=job.use_memory_context or db_job.use_memory_context,
                db_info=job.db_info if job.db_info is not None else db_job.db_info,
            )

        await self.supervise(handle, LegacyEventAdapter(handle.run_id), job)

    async def _pause_run(
        self,
        handle: RunHandle,
        status: str,
        *,
        reason: str = "",
        actions: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """恢复检查路由到暂停态（waiting_approval / needs_reconciliation）。"""
        actions = actions or []
        if status == STATUS_WAITING_APPROVAL:
            handle.pending_actions = actions
            handle.transition(STATUS_WAITING_APPROVAL)
            await self.service._record_approval_requests(handle.run_id, actions)
            await self.service._commit(handle, status=STATUS_WAITING_APPROVAL)
            await handle.bus.publish(RuntimeEvent(
                type=APPROVAL_REQUIRED, run_id=handle.run_id,
                data={"actions": actions},
            ))
            await handle.bus.publish(RuntimeEvent(
                type=RUN_WAITING_APPROVAL, run_id=handle.run_id,
                data={"actions": redact_sensitive(actions)},
            ))
        else:
            handle.transition(STATUS_NEEDS_RECONCILIATION)
            await self.service._commit(
                handle, status=STATUS_NEEDS_RECONCILIATION, recovery_error=reason,
            )
        logger.info("run %s 恢复检查后进入 %s：%s", handle.run_id, status, reason or "-")

    # ── 执行监督：心跳续租 + fencing + 释放 ──────────────────────────────

    def supervise(self, handle: RunHandle, adapter: LegacyEventAdapter, job: RunJob) -> asyncio.Task:
        """驱动执行并提供租约保护。返回监督任务（已登记，参与优雅关闭）。"""
        return self._spawn(self._supervise(handle, adapter, job))

    async def _supervise(
        self, handle: RunHandle, adapter: LegacyEventAdapter, job: RunJob
    ) -> None:
        lease_version = handle.lease_version
        self._active[handle.run_id] = handle
        drive_task = asyncio.create_task(self.service._drive(
            handle,
            adapter,
            messages=job.messages,
            resume=job.resume,
            agent_config=job.agent_config,
            use_memory_context=job.use_memory_context,
            db_info=job.db_info,
            user_id=job.user_id,
        ))
        handle.task = drive_task
        hb_task = asyncio.create_task(self._heartbeat_loop(handle, lease_version, drive_task))
        try:
            # 只以驱动任务为完成判据：心跳是辅助任务，它提前结束（Run 已终态/
            # 已暂停时心跳自然收尾）不代表执行结束，不得取消在途/收尾中的驱动
            await asyncio.wait({drive_task})
        finally:
            if not drive_task.done():
                drive_task.cancel()
            if not hb_task.done():
                hb_task.cancel()
            results = await asyncio.gather(drive_task, hb_task, return_exceptions=True)
            await self._release(handle, lease_version)
            self._active.pop(handle.run_id, None)
        exc = drive_task.exception() if not drive_task.cancelled() else None
        if exc is not None:
            if isinstance(exc, RunLeaseLost):
                # fencing 拒绝属于受控终止（已发布 run_suspended），不是崩溃
                logger.warning(
                    "run %s 执行权丢失，驱动受控终止（由有效 Worker/恢复流程接管）: %r",
                    handle.run_id, exc,
                )
            else:
                logger.critical(
                    "run %s 执行驱动以异常结束（租约将到期后由恢复流程接管）: %r",
                    handle.run_id, exc,
                )

    async def _heartbeat_loop(
        self, handle: RunHandle, lease_version: Optional[int], drive_task: asyncio.Task
    ) -> None:
        """续租循环。仅真租约丢失/无法确认时挂起执行：不得再产生新副作用（F-13）。

        Run 已终态 / 已暂停属于执行链路的正常收尾或等待人工，心跳自然结束，
        不得误判为租约丢失（否则会取消正在收尾的执行、悬挂事件流消费者）。
        """
        try:
            while not drive_task.done():
                await asyncio.sleep(self.heartbeat_interval)
                if drive_task.done():
                    return
                result = await self._heartbeat(handle.run_id, lease_version)
                if result in (HEARTBEAT_FINISHED, HEARTBEAT_PAUSED):
                    return
                if result == HEARTBEAT_LOST:
                    logger.warning(
                        "run %s 租约丢失（lease_version=%s），挂起执行",
                        handle.run_id, lease_version,
                    )
                    handle.suspend_requested = True
                    handle.suspend_reason = "lease_lost"
                    drive_task.cancel()
                    return
        except asyncio.CancelledError:
            raise

    async def _heartbeat(self, run_id: str, lease_version: Optional[int]) -> str:
        if lease_version is None:
            return HEARTBEAT_OK
        try:
            async with AsyncSessionLocal() as db:
                result = await RunRepository(db).heartbeat(
                    run_id, self.worker_id, lease_version, self.lease_seconds
                )
                await db.commit()
            return result
        except Exception:
            # 无法确认租约归属时按丢失处理（宁可恢复，不可重复副作用）
            logger.warning("run %s 心跳失败（按租约丢失处理）", run_id, exc_info=True)
            return HEARTBEAT_LOST

    async def _release(self, handle: RunHandle, lease_version: Optional[int]) -> None:
        """释放租约并递增 fencing token（释放失败无碍：租约到期自动过期）。"""
        handle.lease_version = None
        handle.worker_id = None
        if lease_version is None:
            return
        try:
            async with AsyncSessionLocal() as db:
                ok = await RunRepository(db).release(
                    handle.run_id, self.worker_id, lease_version
                )
                await db.commit()
            if not ok:
                logger.info(
                    "run %s 租约释放跳过（lease_version=%s 已失效）",
                    handle.run_id, lease_version,
                )
        except Exception:
            logger.warning("run %s 租约释放失败", handle.run_id, exc_info=True)

    # ── 轮询：领取孤儿 Run（跨进程提交 / 崩溃恢复） ──────────────────────

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                async with AsyncSessionLocal() as db:
                    row = await RunRepository(db).claim_next(
                        self.worker_id, self.lease_seconds
                    )
                    await db.commit()
                if row is not None:
                    logger.info(
                        "Worker %s 领取 run %s（attempt=%s）",
                        self.worker_id, row.id, row.attempt,
                    )
                    self._spawn(self._claim_execute_row(row))
                if time.monotonic() >= self._next_event_cleanup:
                    self._next_event_cleanup = (
                        time.monotonic() + self._event_cleanup_interval
                    )
                    await self._cleanup_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker 轮询失败")
            await asyncio.sleep(self.poll_interval)

    async def _cleanup_events(self) -> None:
        """事件保留策略（FR-07）：只清理终态且超过保留期的 Run 事件。

        失败仅记录日志，不影响执行路径。
        """
        days = int(getattr(settings, "AGENT_EVENT_RETENTION_DAYS", 30))
        try:
            async with AsyncSessionLocal() as db:
                deleted = await EventRepository(db).cleanup(retention_days=days)
                await db.commit()
            if deleted:
                logger.info("事件保留清理：%s 行（保留 %s 天）", deleted, days)
        except Exception:
            logger.exception("事件保留清理失败（不影响执行）")

    async def _claim_execute_row(self, row: Any) -> None:
        try:
            await self.run_claimed(row, self._job_from_row(row))
        except Exception:
            logger.exception("Worker 驱动 run %s 异常", row.id)

    @staticmethod
    def _job_from_row(row: Any) -> RunJob:
        payload = row.input_payload or {}
        return RunJob(
            run_id=row.id,
            user_id=row.user_id,
            messages=payload.get("messages"),
            agent_config=row.agent_config_snapshot,
            use_memory_context=payload.get("use_memory_context", ""),
            db_info=payload.get("db_info"),
        )


def get_run_worker() -> RunWorker:
    """进程级 Worker 单例（与 AgentRuntimeService 同生命周期）。"""
    from .service import get_runtime_service

    service = get_runtime_service()
    if service.worker is None:
        service.worker = RunWorker(service)
    return service.worker

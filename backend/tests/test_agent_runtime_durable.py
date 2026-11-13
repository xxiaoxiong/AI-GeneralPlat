"""Phase 2 Durable Execution 测试：数据持久化 / 幂等 / 租约 CAS / 工具账本 / 事件日志。

运行：
    pytest backend/tests/test_agent_runtime_durable.py -q
"""
import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
import uuid
from typing import Any, List

import pytest
import pytest_asyncio
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentApproval, AgentRun, AgentRunEvent, AgentSession, AgentToolExecution
from app.models.user import User
from app.services.agent_runtime.lifecycle import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_LIMIT_EXCEEDED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    RunExecutionLease,
    RunHandle,
    RunRegistry,
)
from app.services.agent_runtime.repositories import (
    HEARTBEAT_FINISHED,
    HEARTBEAT_LOST,
    HEARTBEAT_OK,
    HEARTBEAT_PAUSED,
    EventRepository,
    IdempotencyConflict,
    RunRepository,
    ToolExecutionRepository,
)
from app.services.agent_runtime.repositories.errors import (
    IllegalTransition,
    RunLeaseLost,
)
from app.services.agent_runtime.repositories.tool_execution_repository import (
    TOOL_EXEC_EXECUTING,
    TOOL_EXEC_NEEDS_RECONCILIATION,
    TOOL_EXEC_PREPARED,
    TOOL_EXEC_SUCCEEDED,
    TOOL_EXEC_UNKNOWN,
)
from app.services.agent_runtime.context import RunScope, RuntimeContext
from app.services.agent_runtime.events import (
    MODEL_DELTA,
    RUN_COMPLETED,
    RUN_STARTED,
    EventBus,
    LegacyEventAdapter,
    RuntimeEvent,
)
from app.services.agent_runtime.service import (
    AgentRuntimeService,
    ApprovalConflict,
    ApprovalForbidden,
    ApprovalInvalid,
)
from app.services.agent_runtime.tools.adapter import ToolAdapterFactory
from app.services.agent_runtime.tools.policy import ToolPolicy
from app.services.agent_runtime.worker import RunJob, RunWorker


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    # 测试间清空持久化表，保证 claim/扫描类断言不受其他用例残留影响
    async with AsyncSessionLocal() as s:
        for model in (AgentRunEvent, AgentToolExecution, AgentApproval, AgentRun):
            await s.execute(delete(model))
        # 真实 PG/MySQL 强制外键：seed agent/user 行，保证 agent_runs.agent_id/user_id 引用有效
        # （SQLite 默认不强制 FK，故此 seed 仅在生产库回归时起作用；不动任何断言）
        if await s.get(Agent, 1) is None:
            s.add(Agent(id=1, name="durable-agent"))
        if await s.get(User, 1) is None:
            s.add(User(id=1, username="u1", email="u1@test.local", hashed_password="x"))
        await s.commit()
    yield
    await engine.dispose()


def _uid() -> str:
    return uuid.uuid4().hex


async def _create_run(repo: RunRepository, **kw):
    run_id = _uid()
    defaults = dict(
        run_id=run_id,
        agent_id=1,
        session_id=None,
        user_id=1,
        thread_id=f"agent-1-session-{run_id[:8]}",
    )
    defaults.update(kw)
    # 真实 PG/MySQL 有外键约束：确保 agent / user 行存在
    if await repo.session.get(Agent, 1) is None:
        repo.session.add(Agent(id=1, name="durable-agent"))
        await repo.session.flush()
    uid = defaults.get("user_id")
    if uid is not None and await repo.session.get(User, uid) is None:
        repo.session.add(User(
            id=uid, username=f"u{uid}", email=f"u{uid}@test.local",
            hashed_password="x",
        ))
        await repo.session.flush()
    run, created = await repo.create_run(**defaults)
    return run, created


async def _claimed_run(worker_id: str = "w-tool"):
    """创建真实 AgentRun 并 claim 执行权 -> (run_id, lease_version)。

    P0-03：Tool Ledger 全部写入强制校验执行权，测试必须持有真实有效租约。
    """
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, worker_id, 30)
        await s.commit()
    return run.id, version


def _lease_for(run_id: str, version: int, worker_id: str = "w-tool") -> RunExecutionLease:
    return RunExecutionLease(run_id=run_id, worker_id=worker_id, lease_version=version)


# ── FR-01 Run 持久化与幂等创建 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_run_persists_pending():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, created = await _create_run(repo, idempotency_key=None)
        assert created and run.status == STATUS_PENDING
        await s.commit()

        got = await RunRepository(s).get_run(run.id)
        assert got is not None and got.status == STATUS_PENDING


@pytest.mark.asyncio
async def test_idempotent_reuse_same_request_hash():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        r1, c1 = await _create_run(
            repo, user_id=7, idempotency_key="k1", request_hash="h1"
        )
        await s.commit()
        r2, c2 = await _create_run(
            repo, user_id=7, idempotency_key="k1", request_hash="h1"
        )
        assert c1 and not c2 and r1.id == r2.id


@pytest.mark.asyncio
async def test_idempotent_conflict_different_request_hash():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        await _create_run(repo, user_id=7, idempotency_key="k2", request_hash="h1")
        await s.commit()
        with pytest.raises(IdempotencyConflict):
            await _create_run(repo, user_id=7, idempotency_key="k2", request_hash="h2")


@pytest.mark.asyncio
async def test_no_idempotency_key_creates_new_runs():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        r1, _ = await _create_run(repo)
        r2, _ = await _create_run(repo)
        assert r1.id != r2.id


@pytest.mark.asyncio
async def test_idempotency_unique_per_user():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        r1, _ = await _create_run(repo, user_id=1, idempotency_key="shared")
        r2, _ = await _create_run(repo, user_id=2, idempotency_key="shared")
        assert r1.id != r2.id  # 不同用户作用域互不影响


@pytest.mark.asyncio
async def test_db_enforces_idempotency_uniqueness():
    """数据库唯一约束兜底：绕过仓库直接插重复 (user_id, idempotency_key) 必须失败。"""
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo, user_id=3, idempotency_key="dup")
        run_id = run.id
        await s.commit()
        s.add(
            AgentRun(
                id=_uid(), agent_id=1, thread_id="t", status="pending",
                user_id=3, idempotency_key="dup",
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()
        # 旧记录仍在
        assert await RunRepository(s).get_run(run_id) is not None


# ── FR-02 租约 / fencing token ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_only_one_winner():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()

        v1 = await RunRepository(s).claim(run.id, "worker-a", 30)
        v2 = await RunRepository(s).claim(run.id, "worker-b", 30)
        assert v1 is not None and v2 is None

        row = await RunRepository(s).get_run(run.id)
        assert row.worker_id == "worker-a" and row.status == STATUS_RUNNING
        assert row.attempt == 1  # 首次 Claim 计 1 次执行尝试


@pytest.mark.asyncio
async def test_claim_blocked_while_lease_valid_then_recoverable():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        v1 = await RunRepository(s).claim(run.id, "worker-a", 300)
        assert v1 == 1

        # Lease 有效期间不可被抢占
        assert await RunRepository(s).claim(run.id, "worker-b", 300) is None

        # 模拟 Lease 过期（时间戳拨回）
        row = await RunRepository(s).get_run(run.id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

        v2 = await RunRepository(s).claim(run.id, "worker-b", 300)
        assert v2 == 2 and v2 != v1  # fencing token 递增
        row = await RunRepository(s).get_run(run.id)
        assert row.worker_id == "worker-b" and row.attempt == 2


@pytest.mark.asyncio
async def test_heartbeat_extends_lease_and_fencing():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        v1 = await RunRepository(s).claim(run.id, "worker-a", 30)

        assert await RunRepository(s).heartbeat(run.id, "worker-a", v1, 30) == HEARTBEAT_OK
        # 错误版本 / 错误 worker → 真租约丢失（fencing 失败）
        assert await RunRepository(s).heartbeat(run.id, "worker-a", v1 + 5, 30) == HEARTBEAT_LOST
        assert await RunRepository(s).heartbeat(run.id, "worker-z", v1, 30) == HEARTBEAT_LOST


@pytest.mark.asyncio
async def test_heartbeat_three_state_semantics():
    """heartbeat 必须区分 续租成功 / Run 已终态 / Run 已暂停 / 真租约丢失（任务书 P0-02）。

    后三者若被误判为租约丢失，会取消正在收尾的执行、吞掉终止事件并悬挂
    所有事件流消费者（曾导致 t09 多轮会话测试永久挂起）。
    """
    # 已终态 → HEARTBEAT_FINISHED（不是租约丢失）
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        v1 = await RunRepository(s).claim(run.id, "worker-a", 30)
        assert await RunRepository(s).update_status(
            run.id, status=STATUS_COMPLETED,
            lease=RunExecutionLease(run_id=run.id, worker_id="worker-a", lease_version=v1),
        )
        await s.commit()
        assert await RunRepository(s).heartbeat(run.id, "worker-a", v1, 30) == HEARTBEAT_FINISHED

    # 已暂停（needs_reconciliation）→ HEARTBEAT_PAUSED
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        v1 = await RunRepository(s).claim(run.id, "worker-a", 30)
        assert await RunRepository(s).update_status(
            run.id, status=STATUS_NEEDS_RECONCILIATION,
            lease=RunExecutionLease(run_id=run.id, worker_id="worker-a", lease_version=v1),
        )
        await s.commit()
        assert await RunRepository(s).heartbeat(run.id, "worker-a", v1, 30) == HEARTBEAT_PAUSED

    # running 但 lease 易主 → HEARTBEAT_LOST
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        await RunRepository(s).claim(run.id, "worker-a", 30)
        assert await RunRepository(s).heartbeat(run.id, "worker-b", 99, 30) == HEARTBEAT_LOST


@pytest.mark.asyncio
async def test_release_invalidates_old_worker():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        v1 = await RunRepository(s).claim(run.id, "worker-a", 30)
        assert await RunRepository(s).release(run.id, "worker-a", v1)

        # release 后旧 fencing token 不能再提交状态（F-13 / LF-03）
        with pytest.raises(RunLeaseLost):
            await RunRepository(s).update_status(
                run.id, status=STATUS_COMPLETED,
                lease=RunExecutionLease(run_id=run.id, worker_id="worker-a", lease_version=v1),
            )
        row = await RunRepository(s).get_run(run.id)
        assert row.status == STATUS_RUNNING  # 状态未被旧 Worker 篡改


@pytest.mark.asyncio
async def test_update_status_fencing_requires_match():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        v1 = await RunRepository(s).claim(run.id, "worker-a", 30)

        assert await RunRepository(s).update_status(
            run.id, status=STATUS_COMPLETED,
            lease=RunExecutionLease(run_id=run.id, worker_id="worker-a", lease_version=v1),
            final_text="done", summary={"model_calls": 1}, finished=True,
        )
        row = await RunRepository(s).get_run(run.id)
        assert row.status == STATUS_COMPLETED and row.final_text == "done"
        assert row.finished_at is not None


@pytest.mark.asyncio
async def test_claim_next_skips_paused_statuses():
    """waiting_approval / needs_reconciliation 不得被普通 Worker 领取（FR-02.9）。"""
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        waiting, _ = await _create_run(repo)
        waiting.status = STATUS_WAITING_APPROVAL
        recon, _ = await _create_run(repo)
        recon.status = STATUS_NEEDS_RECONCILIATION
        ready, _ = await _create_run(repo)
        await s.commit()

        claimed = await RunRepository(s).claim_next("worker-x", 30)
        assert claimed is not None and claimed.id == ready.id
        assert await RunRepository(s).claim_next("worker-y", 30) is None


@pytest.mark.asyncio
async def test_claim_next_picks_expired_running_for_recovery():
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await s.commit()
        await RunRepository(s).claim(run.id, "dead-worker", 300)
        row = await RunRepository(s).get_run(run.id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

        stale = await RunRepository(s).list_stale_running()
        assert [r.id for r in stale] == [run.id]

        claimed = await RunRepository(s).claim_next("worker-b", 30)
        assert claimed is not None and claimed.id == run.id
        assert claimed.worker_id == "worker-b"


# ── FR-05 Tool Execution Ledger ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ledger_begin_is_idempotent_per_tool_call_id():
    """稳定身份是 (run_id, tool_call_id)：重复登记返回同一记录。"""
    run_id, version = await _claimed_run()
    lease = _lease_for(run_id, version)
    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        rec1, c1 = await ledger.begin(
            run_id=run_id, tool_call_id="call-1", tool_name="http_post",
            args_hash="h", idempotency_key="idem-1", lease=lease,
        )
        rec2, c2 = await ledger.begin(
            run_id=run_id, tool_call_id="call-1", tool_name="http_post",
            args_hash="h", idempotency_key="idem-1", lease=lease,
        )
        assert c1 and not c2 and rec1.id == rec2.id

        # 相同参数、不同 tool_call_id 是两次独立操作（禁止 args_hash 当调用 ID）
        rec3, c3 = await ledger.begin(
            run_id=run_id, tool_call_id="call-2", tool_name="http_post",
            args_hash="h", idempotency_key="idem-2", lease=lease,
        )
        assert c3 and rec3.id != rec1.id


@pytest.mark.asyncio
async def test_ledger_success_flow():
    run_id, version = await _claimed_run()
    lease = _lease_for(run_id, version)
    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="c1", tool_name="t", lease=lease
        )
        assert rec.status == TOOL_EXEC_PREPARED

        assert await ledger.mark_executing(rec.id, lease=lease)
        assert await ledger.mark_succeeded(
            rec.id, result_payload={"ok": True}, external_operation_id="op-9", lease=lease
        )
        row = await ledger.get(run_id, "c1")
        assert row.status == TOOL_EXEC_SUCCEEDED
        assert row.attempt_count == 1
        assert row.result_payload == {"ok": True}
        assert row.external_operation_id == "op-9"
        assert row.finished_at is not None


@pytest.mark.asyncio
async def test_ledger_succeeded_is_immutable():
    """P0-03：成功记录不可被失败覆盖 —— 状态迁移被拒必须显式抛出。"""
    run_id, version = await _claimed_run()
    lease = _lease_for(run_id, version)
    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="c1", tool_name="t", lease=lease
        )
        await ledger.mark_executing(rec.id, lease=lease)
        await ledger.mark_succeeded(rec.id, result_payload={"ok": True}, lease=lease)

        with pytest.raises(IllegalTransition):
            await ledger.mark_failed(rec.id, error="late failure", lease=lease)
        with pytest.raises(IllegalTransition):
            await ledger.mark_succeeded(rec.id, result_payload={"ok": False}, lease=lease)
        row = await ledger.get(run_id, "c1")
        assert row.status == TOOL_EXEC_SUCCEEDED
        assert row.result_payload == {"ok": True}


@pytest.mark.asyncio
async def test_ledger_unknown_blocks_replay():
    """结果未知的外部调用：不得自动重放，进入 needs_reconciliation（FR-05/原则 E）。"""
    run_id, version = await _claimed_run()
    lease = _lease_for(run_id, version)
    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="c1", tool_name="http_post", lease=lease
        )
        await ledger.mark_executing(rec.id, lease=lease)
        await ledger.mark_unknown(rec.id, error="响应超时，远端结果未知", lease=lease)

        assert await ledger.has_unsafe_entries(run_id)
        unresolved = await ledger.list_unresolved(run_id)
        assert [u.status for u in unresolved] == [TOOL_EXEC_UNKNOWN]

        await ledger.mark_needs_reconciliation(rec.id, error="等待人工核对", lease=lease)
        row = await ledger.get(run_id, "c1")
        assert row.status == TOOL_EXEC_NEEDS_RECONCILIATION
        assert await ledger.has_unsafe_entries(run_id)


@pytest.mark.asyncio
async def test_ledger_no_unsafe_after_success():
    run_id, version = await _claimed_run()
    lease = _lease_for(run_id, version)
    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="c1", tool_name="t", lease=lease
        )
        await ledger.mark_executing(rec.id, lease=lease)
        await ledger.mark_succeeded(rec.id, lease=lease)
        assert not await ledger.has_unsafe_entries(run_id)


# ── FR-07 事件日志与序号 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_seq_monotonic_per_run():
    async with AsyncSessionLocal() as s:
        runs = RunRepository(s)
        events = EventRepository(s)
        run, _ = await _create_run(runs)
        await s.commit()

        seqs = [
            await events.append(run.id, "model_delta", {"text": f"t{i}"})
            for i in range(5)
        ]
        await s.commit()
        assert seqs == [1, 2, 3, 4, 5]
        assert await events.last_seq(run.id) == 5


@pytest.mark.asyncio
async def test_event_unique_constraint_on_sequence():
    async with AsyncSessionLocal() as s:
        runs = RunRepository(s)
        events = EventRepository(s)
        run, _ = await _create_run(runs)
        await events.append(run.id, "run_started", {})
        await s.commit()

        s.add(AgentRunEvent(run_id=run.id, sequence=1, event_type="dup"))
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()


@pytest.mark.asyncio
async def test_event_replay_after_seq():
    async with AsyncSessionLocal() as s:
        runs = RunRepository(s)
        events = EventRepository(s)
        run, _ = await _create_run(runs)
        for i in range(30):
            await events.append(run.id, "model_delta", {"i": i})
        await s.commit()

        replay = await EventRepository(s).list_after(run.id, after_seq=12)
        assert [e.sequence for e in replay] == list(range(13, 31))
        tail = await EventRepository(s).list_after(run.id, after_seq=29)
        assert [e.sequence for e in tail] == [30]


@pytest.mark.asyncio
async def test_event_seq_unknown_run_raises():
    async with AsyncSessionLocal() as s:
        with pytest.raises(LookupError):
            await EventRepository(s).append("no-such-run", "run_started", {})


@pytest.mark.asyncio
async def test_event_cleanup_only_terminal_and_expired():
    async with AsyncSessionLocal() as s:
        runs = RunRepository(s)
        events = EventRepository(s)
        done_run, _ = await _create_run(runs)
        await runs.claim(done_run.id, "w", 30)
        await runs.update_status(
            done_run.id, status=STATUS_COMPLETED,
            lease=RunExecutionLease(run_id=done_run.id, worker_id="w", lease_version=1),
            finished=True,
        )
        live_run, _ = await _create_run(runs)
        live_run.status = STATUS_WAITING_APPROVAL
        await s.commit()
        # 把终态 run 的 finished_at 拨老
        done = await runs.get_run(done_run.id)
        done.finished_at = done.finished_at.replace(year=2000)

        await events.append(done_run.id, "run_completed", {})
        await events.append(live_run.id, "run_started", {})
        await s.commit()

        deleted = await EventRepository(s).cleanup(retention_days=30)
        await s.commit()
        assert deleted == 1
        # 运行中的 run 事件保留
        kept = await events.list_after(live_run.id, 0)
        assert len(kept) == 1


# ── 生命周期状态机 ─────────────────────────────────────────────────────────


def test_needs_reconciliation_transitions():
    assert STATUS_NEEDS_RECONCILIATION in ALLOWED_TRANSITIONS[STATUS_RUNNING]
    assert ALLOWED_TRANSITIONS[STATUS_NEEDS_RECONCILIATION] == {
        STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED
    }
    handle = RunHandle(
        run_id="r", agent_id=1, session_id=None, user_id=1, thread_id="t"
    )
    handle.status = STATUS_NEEDS_RECONCILIATION
    with pytest.raises(InvalidTransition):
        handle.transition(STATUS_COMPLETED)  # 不得自动完成
    handle.transition(STATUS_RUNNING)  # 人工确认后可继续


# ── Alembic 迁移（SQLite 完整 upgrade / downgrade 验证） ───────────────────


def test_alembic_migration_upgrade_downgrade(tmp_path):
    db_path = tmp_path / "mig.db"
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = f"sqlite:///{db_path}"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    env.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run(*args):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=backend_dir, env=env, capture_output=True, text=True, timeout=180,
        )
        assert r.returncode == 0, f"alembic {args} failed:\n{r.stdout}\n{r.stderr}"

    run("upgrade", "head")

    con = sqlite3.connect(db_path)
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"agent_tool_executions", "agent_run_events"} <= tables

        run_cols = {
            r[1] for r in con.execute("PRAGMA table_info(agent_runs)").fetchall()
        }
        for col in (
            "request_hash", "input_payload", "agent_config_snapshot", "started_at",
            "worker_id", "lease_expires_at", "heartbeat_at", "attempt",
            "lease_version", "last_event_seq", "recovery_error", "recovery_metadata",
        ):
            assert col in run_cols, f"agent_runs 缺少列 {col}"

        appro_cols = {
            r[1] for r in con.execute("PRAGMA table_info(agent_approvals)").fetchall()
        }
        for col in ("action_id", "tool_call_id", "args_hash", "action_payload", "decision_status"):
            assert col in appro_cols, f"agent_approvals 缺少列 {col}"

        # 唯一约束存在（SQLite 不保留约束名，按列组合验证）
        def unique_index_cols(table):
            cols_sets = []
            for idx in con.execute(f"PRAGMA index_list({table})").fetchall():
                if idx[2]:  # unique
                    icols = [
                        r[2] for r in con.execute(f"PRAGMA index_info({idx[1]})").fetchall()
                    ]
                    cols_sets.append(icols)
            return cols_sets

        assert ["user_id", "idempotency_key"] in unique_index_cols("agent_runs")
        assert ["run_id", "sequence"] in unique_index_cols("agent_run_events")
        assert ["run_id", "tool_call_id"] in unique_index_cols("agent_tool_executions")
    finally:
        con.close()

    run("downgrade", "base")
    run("upgrade", "head")  # 回滚后可再次升级


# ════════════════════════════════════════════════════════════════════════════
# Phase 2：Worker（Claim / Lease / Heartbeat / Fencing / 恢复 / 优雅关闭）
# ════════════════════════════════════════════════════════════════════════════


def _make_service(
    *,
    worker_id="w-test",
    lease_seconds=30,
    heartbeat_interval=0.05,
    poll_interval=0.02,
    shutdown_grace=0.5,
):
    svc = AgentRuntimeService()
    svc.worker = RunWorker(
        svc,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        heartbeat_interval=heartbeat_interval,
        poll_interval=poll_interval,
        shutdown_grace=shutdown_grace,
    )
    return svc


def _stub_drive(record, *, block=False):
    """替换 _drive：记录调用参数（不触发模型）；block=True 时阻塞直到被取消。"""

    async def fake_drive(
        self, run, adapter, *, messages, resume,
        agent_config=None, use_memory_context="", db_info=None, user_id=None,
    ):
        record.append({"run_id": run.run_id, "messages": messages, "resume": resume})
        await run.bus.publish(RuntimeEvent(type=RUN_STARTED, run_id=run.run_id))
        if block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                record.append({"run_id": run.run_id, "cancelled": True})
                raise
        run.final_text = "ok"
        run.summary = {"tool_calls": 0, "model_calls": 1}
        run.transition(STATUS_COMPLETED)
        await self._commit(
            run, status=STATUS_COMPLETED, final_text="ok",
            summary=run.summary, finished=True,
        )
        await run.bus.publish(
            RuntimeEvent(type=RUN_COMPLETED, run_id=run.run_id, data={"final_text": "ok"})
        )

    return fake_drive


async def _wait_status(run_id, statuses, timeout=5.0):
    """轮询等待 run 达到指定状态，返回最终行（超时返回 None）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with AsyncSessionLocal() as s:
            row = await RunRepository(s).get_run(run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.02)
    return None


# ── T-06 多 Worker 并发领取 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t06_two_workers_single_claim_winner():
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id

    wa, wb = _make_service(worker_id="wa"), _make_service(worker_id="wb")
    rows = await asyncio.gather(
        wa.worker.claim_run(run_id),
        wb.worker.claim_run(run_id),
    )
    claimed = [r for r in rows if r is not None]
    assert len(claimed) == 1  # 只有一个 Worker 获得执行权
    assert claimed[0].lease_version == 1 and claimed[0].attempt == 1


@pytest.mark.asyncio
async def test_t06_two_runs_each_executed_once_across_workers(monkeypatch):
    record = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))

    async with AsyncSessionLocal() as s:
        r1, _ = await _create_run(RunRepository(s))
        r2, _ = await _create_run(RunRepository(s))
        await s.commit()
        ids = {r1.id, r2.id}

    wa, wb = _make_service(worker_id="wa"), _make_service(worker_id="wb")
    await wa.worker.start()
    await wb.worker.start()
    try:
        for rid in ids:
            row = await _wait_status(rid, {STATUS_COMPLETED})
            assert row is not None, f"run {rid} 未被执行"
    finally:
        await wa.worker.stop()
        await wb.worker.stop()

    executed = [r["run_id"] for r in record]
    assert sorted(executed) == sorted(ids)  # 每个 run 恰好执行一次，无重复/遗漏


# ── T-07 租约丢失 / fencing ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t07_lease_loss_cancels_drive_without_state_write(monkeypatch):
    record = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record, block=True))
    svc = _make_service(worker_id="wa", heartbeat_interval=0.05)

    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id

    row = await svc.worker.claim_run(run_id)
    assert row is not None
    task = asyncio.create_task(svc.worker.run_claimed(row, RunJob(run_id=run_id)))
    while not record:  # 等待执行真正开始
        await asyncio.sleep(0.01)

    # 模拟租约易主：另一 Worker 接管（fencing token 递增）
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(lease_version=AgentRun.lease_version + 1, worker_id="wb")
        )
        await s.commit()

    await asyncio.wait_for(task, timeout=5)  # 心跳失败 → 挂起执行并结束
    assert any(r.get("cancelled") for r in record)  # 执行已被中止

    async with AsyncSessionLocal() as s:
        got = await RunRepository(s).get_run(run_id)
    assert got.status == STATUS_RUNNING  # 挂起不得写 cancelled
    assert got.worker_id == "wb"  # 状态归属未被旧 Worker 篡改
    # 旧 Worker 的 fencing token 已失效，无法再领取
    assert await svc.worker.claim_run(run_id) is None


# ── 重启恢复：孤儿 Run 接管（attempt>1 恢复检查） ──────────────────────────


@pytest.mark.asyncio
async def test_recovery_orphan_run_reclaimed_and_executed(monkeypatch):
    record = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-recover")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(
            repo,
            input_payload={
                "messages": [{"role": "user", "content": "hi"}],
                "use_memory_context": "",
                "db_info": None,
            },
            agent_config_snapshot={"id": 1, "tools": []},
        )
        await repo.claim(run.id, "dead-worker", 30)
        await s.commit()
        run_id = run.id
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)  # 租约过期
        await s.commit()

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_COMPLETED})
    finally:
        await svc.worker.stop()
    assert done is not None
    assert done.attempt == 2  # 重新领取计一次恢复尝试
    assert record[0]["messages"] == [{"role": "user", "content": "hi"}]  # 输入快照重建


@pytest.mark.asyncio
async def test_recovery_unsafe_ledger_blocks_auto_replay():
    svc = _make_service(worker_id="w-recover")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, "dead-worker", 30)
        run_id = run.id
        lease = _lease_for(run_id, version, worker_id="dead-worker")
        # 崩溃在副作用结果未知的窗口：账本记录为 unknown（写入时执行权有效）。
        # webhook_notify 无下游幂等能力 -> 禁止自动重放（仅工具契约声明下游
        # 幂等的 external_idempotent 工具允许复用同一幂等键重试，见 T-05）
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="call_1", tool_name="webhook_notify",
            args_snapshot={"url": "https://hooks.example.com/x"},
            lease=lease,
        )
        await ledger.mark_executing(rec.id, lease=lease)
        await ledger.mark_unknown(rec.id, error="网络超时，远端是否成功未知", lease=lease)
        await s.commit()
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)  # 租约过期
        await s.commit()

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_NEEDS_RECONCILIATION})
    finally:
        await svc.worker.stop()
    assert done is not None
    assert done.recovery_error and "禁止自动重放" in done.recovery_error


@pytest.mark.asyncio
async def test_recovery_checkpoint_interrupt_returns_to_waiting_approval(monkeypatch):
    svc = _make_service(worker_id="w-recover")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await repo.claim(run.id, "dead-worker", 30)
        await s.commit()
        run_id = run.id
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

    async def fake_thread_state(thread_id):
        return {
            "next": ["hitl"],
            "has_interrupt": True,
            "pending_actions": [
                {"tool": "delete_file", "args": {"path": "/x"}, "description": ""},
            ],
        }

    monkeypatch.setattr(svc, "get_thread_state", fake_thread_state)

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_WAITING_APPROVAL})
    finally:
        await svc.worker.stop()
    assert done is not None

    # 恢复审批所需的审批记录已持久化
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AgentApproval).where(AgentApproval.run_id == run_id)
        )).scalars().all()
    assert len(rows) == 1 and rows[0].tool_name == "delete_file"


# ── F-06 幂等重复请求 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f06_idempotent_duplicate_request_single_execution(monkeypatch):
    record = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-idem")
    cfg = {"id": 1, "tools": [], "model_config_id": None}
    msgs = [{"role": "user", "content": "hi"}]

    run_id_1, stream1 = await svc.start_run(
        agent_config=cfg, messages=msgs, user_id=1,
        session_id=None, idempotency_key="idem-1",
    )
    events1 = [e async for e in stream1]
    assert events1[-1]["type"] == "done" and events1[-1]["status"] == "completed"

    run_id_2, stream2 = await svc.start_run(
        agent_config=cfg, messages=msgs, user_id=1,
        session_id=None, idempotency_key="idem-1",
    )
    events2 = [e async for e in stream2]

    assert run_id_2 == run_id_1  # 不产生重复 Run
    assert len(record) == 1      # 不重复执行
    assert any(e.get("type") == "final" for e in events2)  # 回放补齐终态事件
    assert events2[-1]["type"] == "done" and events2[-1]["status"] == STATUS_COMPLETED

    async with AsyncSessionLocal() as s:
        got = await RunRepository(s).get_run(run_id_1)
    assert got.status == STATUS_COMPLETED and got.final_text == "ok"


# ── 优雅关闭：挂起而非取消（保留可恢复状态） ───────────────────────────────


@pytest.mark.asyncio
async def test_graceful_shutdown_suspends_and_releases_lease(monkeypatch):
    record = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record, block=True))
    svc = _make_service(worker_id="w-shut", shutdown_grace=0.1)

    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id

    row = await svc.worker.claim_run(run_id)
    assert row is not None
    task = asyncio.create_task(svc.worker.run_claimed(row, RunJob(run_id=run_id)))
    while not record:
        await asyncio.sleep(0.01)

    await svc.worker.stop()  # 优雅关闭：等待超时后挂起在途执行
    await asyncio.gather(task, return_exceptions=True)
    assert any(r.get("cancelled") for r in record)

    async with AsyncSessionLocal() as s:
        got = await RunRepository(s).get_run(run_id)
    assert got.status == STATUS_RUNNING     # 挂起不写 cancelled
    assert got.worker_id is None            # 租约已释放

    # 其他 Worker 可立即接管（不需要等租约过期）
    other = _make_service(worker_id="w-next")
    assert await other.worker.claim_run(run_id) is not None


@pytest.mark.asyncio
async def test_drive_suspend_keeps_run_recoverable(monkeypatch):
    """真实 _drive 的挂起分支：取消时不改写状态（F-02 / F-13 状态保护）。"""
    monkeypatch.setattr(
        AgentRuntimeService, "_load_model",
        lambda self, mid: _coro(({"provider": "mock"}, {"name": "mock"})),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.service.build_agent",
        lambda *a, **k: object(),
    )

    async def fake_consume(self, agent, payload, run_config, ctx, run, bus):
        await asyncio.Event().wait()  # 一直执行

    monkeypatch.setattr(AgentRuntimeService, "_consume_stream", fake_consume)

    svc = _make_service(worker_id="w-suspend")
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
    row = await svc.worker.claim_run(run_id)
    handle = svc.registry.attach(
        run_id=run_id, agent_id=1, session_id=None, user_id=1,
        thread_id=row.thread_id, status=STATUS_RUNNING,
    )
    handle.lease_version = row.lease_version
    handle.suspend_requested = True  # 优雅关闭/租约丢失语义

    task = asyncio.create_task(svc._drive(
        handle, LegacyEventAdapter(run_id),
        messages=[{"role": "user", "content": "x"}], resume=None,
        agent_config={"id": 1, "tools": []}, user_id=1,
    ))
    await asyncio.sleep(0.1)  # 进入 _consume_stream 挂起
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with AsyncSessionLocal() as s:
        got = await RunRepository(s).get_run(run_id)
    assert got.status == STATUS_RUNNING  # 不得写 cancelled，保留可恢复
    assert got.error is None


def _coro(value):
    async def _inner(*a, **k):
        return value
    return _inner()


# ── FR-05 工具账本故障注入（T-04 / T-05） ──────────────────────────────────


def _make_ctx(run_id: str, worker_id: str = "w-tool", lease_version: int = 1) -> RuntimeContext:
    return RuntimeContext(
        scope=RunScope(user_id=1),
        run_id=run_id,
        thread_id=f"thr-{run_id}",
        agent_id=1,
        worker_id=worker_id,
        lease_version=lease_version,
    )


def _make_adapter(ctx: RuntimeContext, tool_names) -> ToolAdapterFactory:
    policy = ToolPolicy.from_settings(set(tool_names), enable_hitl=False)
    return ToolAdapterFactory(ctx, policy, EventBus())


def _fake_executor(behavior):
    """ToolExecutor 替身：真实账本 + 可注入故障的执行体。"""

    class _Fake:
        def __init__(self, db, agent_config):
            pass

        async def execute(self, name, args, *, raise_on_error=False):
            return await behavior(name, args)

    return _Fake


@pytest.mark.asyncio
async def test_t01_multi_step_tool_ledger_stable_ids_and_keys(monkeypatch):
    """T-01：多步工具 Run 中每个 tool_call 都有稳定 ID、状态和幂等键。"""
    calls = []

    async def behavior(name, args):
        calls.append(name)
        return f"result-of-{name}"

    monkeypatch.setattr(
        "app.services.agent_runtime.tools.adapter.ToolExecutor",
        _fake_executor(behavior),
    )
    run_id, lv = await _claimed_run()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=lv), {"calculator", "web_search"})
    await adapter._execute("calculator", {"tool_call_id": "tc-a", "expression": "1+1"})
    await adapter._execute("web_search", {"tool_call_id": "tc-b", "query": "langgraph"})
    assert calls == ["calculator", "web_search"]

    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        rec_a = await ledger.get(run_id, "tc-a")
        rec_b = await ledger.get(run_id, "tc-b")
    assert rec_a.status == TOOL_EXEC_SUCCEEDED and rec_b.status == TOOL_EXEC_SUCCEEDED
    assert rec_a.tool_call_id != rec_b.tool_call_id  # 稳定且互异的调用 ID
    assert rec_a.args_hash and rec_b.args_hash  # 输入指纹留存
    assert rec_a.args_snapshot["expression"] == "1+1"
    # 幂等键 = sha256(run_id + tool_call_id)，与 args 无关、跨重试稳定
    assert rec_a.idempotency_key == hashlib.sha256(f"{run_id}:tc-a".encode()).hexdigest()
    assert rec_b.idempotency_key == hashlib.sha256(f"{run_id}:tc-b".encode()).hexdigest()
    assert rec_a.idempotency_key != rec_b.idempotency_key


@pytest.mark.asyncio
async def test_t04_tool_success_then_crash_replay_returns_cached_result(monkeypatch):
    """T-04：工具真实执行并写入 succeeded 账本后、Checkpoint 前崩溃；
    重建 Runtime 恢复重放同一 tool_call 时读取账本结果，不重复执行工具。"""
    calls = []

    async def behavior(name, args):
        calls.append(args)
        return "42"

    monkeypatch.setattr(
        "app.services.agent_runtime.tools.adapter.ToolExecutor",
        _fake_executor(behavior),
    )
    run_id, lv = await _claimed_run()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=lv), {"calculator"})
    out1 = await adapter._execute("calculator", {"tool_call_id": "tc-1", "expression": "2+2"})
    assert out1 == "42"
    assert len(calls) == 1

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-1")
    assert rec.status == TOOL_EXEC_SUCCEEDED
    assert rec.result_payload == "42"

    # 模拟崩溃后重建 Runtime（新 ctx/adapter），Graph 重放同一 tool_call
    adapter2 = _make_adapter(_make_ctx(run_id, lease_version=lv), {"calculator"})
    out2 = await adapter2._execute("calculator", {"tool_call_id": "tc-1", "expression": "2+2"})
    assert out2 == "42"  # 返回持久化结果
    assert len(calls) == 1  # 工具不重复执行

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-1")
    assert rec.status == TOOL_EXEC_SUCCEEDED
    assert rec.attempt_count == 1


@pytest.mark.asyncio
async def test_t05_unknown_result_blocks_replay_without_idempotency(monkeypatch):
    """T-05（无下游幂等能力）：外部副作用结果未知 -> unknown；
    自动恢复不得再次执行（不再 POST），进入 needs_reconciliation。"""
    calls = []

    async def behavior(name, args):
        calls.append(args)
        raise TimeoutError("webhook 响应超时，远端是否成功未知")

    monkeypatch.setattr(
        "app.services.agent_runtime.tools.adapter.ToolExecutor",
        _fake_executor(behavior),
    )
    run_id, lv = await _claimed_run()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=lv), {"webhook_notify"})
    out1 = await adapter._execute(
        "webhook_notify", {"tool_call_id": "tc-9", "url": "https://hooks.example.com/x"}
    )
    assert "工具执行错误" in out1
    assert len(calls) == 1

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-9")
    assert rec.status == TOOL_EXEC_UNKNOWN

    # 重建 Runtime 重放：禁止再次执行
    adapter2 = _make_adapter(_make_ctx(run_id, lease_version=lv), {"webhook_notify"})
    out2 = await adapter2._execute(
        "webhook_notify", {"tool_call_id": "tc-9", "url": "https://hooks.example.com/x"}
    )
    assert len(calls) == 1  # 未再次执行、未再次 POST
    assert "阻止自动重放" in out2

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-9")
    assert rec.status == TOOL_EXEC_NEEDS_RECONCILIATION


@pytest.mark.asyncio
async def test_t05_idempotent_retry_reuses_same_key(monkeypatch):
    """T-05（有下游幂等键）：POST 超时结果未知 -> 重试复用同一 Idempotency-Key，
    下游按幂等键去重，逻辑副作用只产生一次。

    P0-04：幂等能力必须由工具契约显式声明（默认 HTTP POST = non_idempotent），
    本用例声明 http_post 下游支持 Idempotency-Key 去重后才允许幂等键重试。
    """
    from app.services.agent_runtime.tools.classification import (
        CLASS_EXTERNAL_IDEMPOTENT,
        RETRY_IDEMPOTENT_KEY,
        SideEffectContract,
        register_side_effect_contract,
        unregister_side_effect_contract,
    )

    register_side_effect_contract(SideEffectContract(
        tool_name="http_post",
        side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
        downstream_idempotency_supported=True,
        idempotency_key_header="Idempotency-Key",
        retry_policy=RETRY_IDEMPOTENT_KEY,
        external_operation_lookup="GET /orders/{Idempotency-Key}",
    ))
    try:
        calls = []
        downstream: dict = {}  # Idempotency-Key -> 下游业务结果

        async def behavior(name, args):
            key = (args.get("headers") or {}).get("Idempotency-Key")
            assert key, "契约声明下游幂等后必须注入 Idempotency-Key"
            calls.append(key)
            if key not in downstream:
                downstream[key] = "HTTP 201 订单已创建"  # 远端已成功
            if len(calls) == 1:
                raise TimeoutError("响应超时，未收到远端响应")  # 客户端超时
            return downstream[key]  # 幂等键命中，返回既有结果

        monkeypatch.setattr(
            "app.services.agent_runtime.tools.adapter.ToolExecutor",
            _fake_executor(behavior),
        )
        monkeypatch.setattr(ToolPolicy, "check_url", lambda self, url: None)  # 跳过 DNS/SSRF 检查
        run_id, lv = await _claimed_run()
        payload = {"tool_call_id": "tc-7", "url": "https://api.example.com/orders", "body": {"sku": "A-1"}}
        adapter = _make_adapter(_make_ctx(run_id, lease_version=lv), {"http_post"})
        out1 = await adapter._execute("http_post", dict(payload))
        assert "工具执行错误" in out1
        assert len(calls) == 1
        assert len(downstream) == 1  # 远端已产生副作用

        async with AsyncSessionLocal() as s:
            rec = await ToolExecutionRepository(s).get(run_id, "tc-7")
        assert rec.status == TOOL_EXEC_UNKNOWN

        # 重试（恢复重放）：external_idempotent 允许复用同一幂等键重试
        adapter2 = _make_adapter(_make_ctx(run_id, lease_version=lv), {"http_post"})
        out2 = await adapter2._execute("http_post", dict(payload))
        assert out2 == "HTTP 201 订单已创建"
        assert calls[0] == calls[1]  # 复用同一 Idempotency-Key
        assert len(downstream) == 1  # 逻辑副作用只产生一次
        # 幂等键 = sha256(run_id + tool_call_id) 摘要，跨重试稳定
        assert calls[0] == hashlib.sha256(f"{run_id}:tc-7".encode()).hexdigest()

        async with AsyncSessionLocal() as s:
            rec = await ToolExecutionRepository(s).get(run_id, "tc-7")
        assert rec.status == TOOL_EXEC_SUCCEEDED
        assert rec.attempt_count == 2
    finally:
        unregister_side_effect_contract("http_post")


# ════════════════════════════════════════════════════════════════════════════
# Phase 4：HITL 审批持久化（T-02 / T-03 / F-11 / FR-04 / FR-06）
# ════════════════════════════════════════════════════════════════════════════


class _FakeChatModel(BaseChatModel):
    """脚本化模型：按序返回预设消息（工具调用轮 -> 恢复后最终回答轮）。"""

    script: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script.pop(0) if self.script else AIMessage(content="（无更多脚本输出）")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _hitl_agent_config():
    return {
        "id": 1,
        "model_config_id": 1,
        "tools": ["webhook_notify"],  # DEFAULT_HITL_TOOLS 内的副作用工具
        "system_prompt": "测试",
    }


async def _fake_load_model(self, model_config_id):
    return (
        {"name": "mock", "base_url": "http://x", "api_key": "k", "extra_config": {}},
        {"name": "fake", "supports_function_call": True},
    )


async def _fake_load_agent_config(self, agent_id):
    return _hitl_agent_config()


def _setup_hitl(monkeypatch, calls):
    """T-02/T-03 共用装配：真实 build_agent 图 + FakeChatModel + webhook_notify。

    首轮模型发出 webhook_notify 工具调用（触发 HITL 中断），批准恢复后
    第二轮给出最终回答。工具执行体为替身（记录调用），真实走工具账本。
    """
    import app.services.agent_runtime.factory as factory_mod

    model = _FakeChatModel(script=[
        AIMessage(content="", tool_calls=[{
            "name": "webhook_notify",
            "args": {"webhook_url": "https://hooks.example.com/x", "message": "hi"},
            "id": "tc-h1",
        }]),
        AIMessage(content="已通知"),
    ])
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)

    async def behavior(name, args):
        calls.append({"name": name, "args": dict(args)})
        return "webhook 已送达"

    monkeypatch.setattr(
        "app.services.agent_runtime.tools.adapter.ToolExecutor",
        _fake_executor(behavior),
    )
    monkeypatch.setattr(AgentRuntimeService, "_load_model", _fake_load_model)
    monkeypatch.setattr(AgentRuntimeService, "_load_agent_config", _fake_load_agent_config)


async def _run_to_waiting_approval(svc):
    """启动 HITL Run 并消费事件流至 waiting_approval，返回 (run_id, events)。"""
    run_id, stream = await svc.start_run(
        agent_config=_hitl_agent_config(),
        messages=[{"role": "user", "content": "发个通知"}],
        user_id=1,
        session_id=None,
    )
    events = [e async for e in stream]
    await svc.worker.stop()  # 执行进程退出（释放租约）
    return run_id, events


@pytest.mark.asyncio
async def test_t02_hitl_approval_persisted_and_resumable_after_restart(monkeypatch):
    """T-02：HITL 审批动作持久化；服务重启丢失内存句柄后凭库中记录恢复执行。

    验收（FR-06 / F-10）：审批前工具零执行；agent_approvals 落库（稳定
    action_id、pending）；重启后 resume 精确恢复；工具恰执行一次且账本
    确认 succeeded、决策落库为 approved。
    """
    calls = []
    _setup_hitl(monkeypatch, calls)

    svc = _make_service(worker_id="w-hitl-1")
    run_id, events = await _run_to_waiting_approval(svc)

    assert events[-1]["type"] == "done" and events[-1]["status"] == STATUS_WAITING_APPROVAL
    assert any(e.get("approval", {}).get("actions") for e in events)  # 审批请求已推送给客户端
    assert not calls, "审批完成前工具不得执行"

    # 审批请求已持久化（FR-06）：稳定 action_id、pending 决策
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AgentApproval).where(AgentApproval.run_id == run_id)
        )).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "webhook_notify"
    assert row.action_id and row.decision_status == "pending"

    # 模拟服务重启：内存句柄全部丢失，仅剩数据库 + Checkpoint
    RunRegistry.instance()._runs.clear()
    svc2 = _make_service(worker_id="w-hitl-2")

    resume_events = [e async for e in svc2.resume_run(
        run_id,
        [{"type": "approve", "message": "ok", "action_id": row.action_id}],
        1,
    )]
    assert resume_events[-1]["type"] == "done"
    assert resume_events[-1]["status"] == STATUS_COMPLETED

    # 工具恰执行一次，账本确认副作用结果
    assert len(calls) == 1 and calls[0]["name"] == "webhook_notify"
    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-h1")
        got = await RunRepository(s).get_run(run_id)
        decided = (await s.execute(
            select(AgentApproval).where(AgentApproval.run_id == run_id)
        )).scalars().one()
    assert rec is not None and rec.status == TOOL_EXEC_SUCCEEDED
    assert got.status == STATUS_COMPLETED
    assert decided.decision == "approve"
    assert decided.decision_status == "approved" and decided.decided_by == 1


@pytest.mark.asyncio
async def test_t03_approval_idempotent_and_conflict_semantics(monkeypatch):
    """T-03（F-11）：重复一致审批幂等；改判 / 非法决策 / 无权审批显式拒绝。

    - 重复一致决策：幂等返回当前状态，不重复恢复、不重复执行副作用
    - 同一动作改判：ApprovalConflict（API 层映射 HTTP 409），不得静默覆盖
    - 未知决策类型 / 未知审批动作：ApprovalInvalid（HTTP 400）
    - 非属主审批：ApprovalForbidden（HTTP 403）
    """
    calls = []
    _setup_hitl(monkeypatch, calls)

    svc = _make_service(worker_id="w-hitl-3")
    run_id, events = await _run_to_waiting_approval(svc)
    assert events[-1]["status"] == STATUS_WAITING_APPROVAL

    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(AgentApproval).where(AgentApproval.run_id == run_id)
        )).scalars().one()
    action_id = row.action_id
    approve = [{"type": "approve", "message": "ok", "action_id": action_id}]

    ev1 = [e async for e in svc.resume_run(run_id, approve, 1)]
    assert ev1[-1]["type"] == "done" and ev1[-1]["status"] == STATUS_COMPLETED
    assert len(calls) == 1

    # 1) 重复一致审批：幂等返回当前状态，不重复恢复、不重复执行
    ev2 = [e async for e in svc.resume_run(run_id, approve, 1)]
    assert ev2[-1].get("idempotent") is True
    assert ev2[-1]["status"] == STATUS_COMPLETED
    assert len(calls) == 1  # 工具不重复执行

    # 2) 同一动作改判：冲突显式拒绝，不得静默覆盖既有决策
    ev3 = [e async for e in svc.resume_run(
        run_id, [{"type": "reject", "message": "改判", "action_id": action_id}], 1,
    )]
    assert ev3[0]["type"] == "error" and ev3[0].get("code") == ApprovalConflict.code

    # 3) 未知决策类型：显式拒绝
    ev4 = [e async for e in svc.resume_run(
        run_id, [{"type": "maybe", "action_id": action_id}], 1,
    )]
    assert ev4[0]["type"] == "error" and ev4[0].get("code") == ApprovalInvalid.code

    # 4) 未知审批动作：显式拒绝
    ev5 = [e async for e in svc.resume_run(
        run_id, [{"type": "approve", "action_id": "no-such-action"}], 1,
    )]
    assert ev5[0]["type"] == "error" and ev5[0].get("code") == ApprovalInvalid.code

    # 5) 非属主用户审批：显式拒绝（check_decisions 为 API 层 403 映射源）
    with pytest.raises(ApprovalForbidden):
        await svc.check_decisions(run_id, approve, 99)

    # 6) 全程未发生改判/重复执行：决策保持 approve，工具仍恰一次
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(AgentApproval).where(AgentApproval.run_id == run_id)
        )).scalars().one()
    assert row.decision == "approve" and row.decision_status == "approved"
    assert len(calls) == 1


# ════════════════════════════════════════════════════════════════════════════
# Phase 5：事件持久化与 SSE 断线回放（T-08 / FR-07）
# ════════════════════════════════════════════════════════════════════════════


async def _publish_scripted_events(run_id: str, n_deltas: int = 29) -> None:
    """模拟执行 Worker 发布 n_deltas 个增量 + 终态事件（持久化总线，FR-07）。"""
    bus = EventBus(persist=True)
    bus.bind_run(thread_id=f"agent-1-run-{run_id}", agent_id=1)
    for i in range(n_deltas):
        await bus.publish(RuntimeEvent(
            type=MODEL_DELTA,
            run_id=run_id,
            data={"text": f"t{i}", "final": False, "index": i},
        ))
    await bus.publish(RuntimeEvent(
        type=RUN_COMPLETED, run_id=run_id, data={"status": "completed"},
    ))


@pytest.mark.asyncio
async def test_t08_sse_replay_after_disconnect_no_loss():
    """T-08（FR-07）：SSE 断线后按 after_seq 续传 —— 不丢失、不重复、不乱序。

    发布并持久化 30 个事件；客户端消费到 seq=12 断开；after_seq=12 重连
    收到 seq=13 到终态。消费端为全新服务实例（内存句柄已清空），验证
    API 实例与执行 Worker 不同进程也能消费完整事件流。
    """
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
    run_id = run.id
    await _publish_scripted_events(run_id, n_deltas=29)  # 持久化 30 个事件

    async with AsyncSessionLocal() as s:
        rows = await EventRepository(s).list_after(run_id, 0)
    assert len(rows) == 30
    assert [r.sequence for r in rows] == list(range(1, 31))  # 序号单调唯一

    # 模拟 API 实例（与发布端不同进程）：全新服务、无内存句柄
    RunRegistry.instance()._runs.clear()
    svc = _make_service(worker_id="w-api")

    first = []
    async for ev in svc.replay_events(run_id):  # 从头消费（含 seq=0）
        first.append(ev)
        if len(first) >= 13:                    # 消费到 seq=12 后断开
            break
    assert [e["seq"] for e in first] == list(range(13))
    assert first[-1]["type"] == "final_chunk"

    resumed = [e async for e in svc.replay_events(run_id, after_seq=12)]
    assert resumed, "after_seq=12 重连必须续传后续事件"
    assert [e["seq"] for e in resumed] == list(range(13, 13 + len(resumed)))
    assert resumed[-1]["type"] == "done" and resumed[-1]["status"] == "completed"

    # 不丢失：两段拼接覆盖全量（final_start + 29 chunk + final + done = 32）
    # 不重复：两段 seq 无交集；不乱序：全局 seq 连续
    all_events = first + resumed
    assert [e["seq"] for e in all_events] == list(range(len(all_events)))
    assert len(all_events) == 32

    # 最终文本完整性：拼接 chunk 与最终 final 全文一致
    chunks = [e["content"] for e in all_events if e["type"] == "final_chunk"]
    assert chunks == [f"t{i}" for i in range(29)]
    assert all_events[-2]["type"] == "final"
    assert all_events[-2]["content"] == "".join(chunks)


# ════════════════════════════════════════════════════════════════════════════
# Batch 6（P1-03 §11 / P1-04 §12）验收
# ════════════════════════════════════════════════════════════════════════════


def _setup_hitl_script(monkeypatch, calls, script):
    """通用 HITL 装配：按给定模型脚本（多轮工具调用→多批次 Interrupt）。"""
    import app.services.agent_runtime.factory as factory_mod

    model = _FakeChatModel(script=list(script))
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)

    async def behavior(name, args):
        calls.append({"name": name, "args": dict(args)})
        return "webhook 已送达"

    monkeypatch.setattr(
        "app.services.agent_runtime.tools.adapter.ToolExecutor",
        _fake_executor(behavior),
    )
    monkeypatch.setattr(AgentRuntimeService, "_load_model", _fake_load_model)
    monkeypatch.setattr(AgentRuntimeService, "_load_agent_config", _fake_load_agent_config)


def _tool_turn(call_id: str):
    return AIMessage(content="", tool_calls=[{
        "name": "webhook_notify",
        "args": {"webhook_url": "https://hooks.example.com/x", "message": call_id},
        "id": call_id,
    }])


async def _consume(svc_stream):
    return [e async for e in svc_stream]


async def _pending_action_ids(run_id):
    """当前未决策（= 当前 Interrupt 批次）的 action_id，按登记顺序（动作顺序）。"""
    from sqlalchemy import select
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AgentApproval.action_id).where(
                AgentApproval.run_id == run_id, AgentApproval.decision.is_(None)
            ).order_by(AgentApproval.id)
        )).scalars().all()
    return [r for r in rows]


# ── §11-2：同一 Run 连续两个 Interrupt，第二次只恢复第二批动作 ─────────────

@pytest.mark.asyncio
async def test_t03b_second_interrupt_batch_isolated(monkeypatch):
    """P1-03 A：两个连续 Interrupt，第二次恢复只带第二批决策（不把历史批次带入）。

    老实现按 Run 全部已决策审批构造 graph_decisions，会在第二次恢复时把第一批
    决策一并塞入 Command(resume)，与第二批 action_requests 数量/位置不齐而失败。
    """
    calls = []
    _setup_hitl_script(monkeypatch, calls, [
        _tool_turn("tc-h1"),          # 批次 1：单动作
        _tool_turn("tc-h2"),          # 批次 2：单动作（第一批执行后再触发）
        AIMessage(content="全部完成"),
    ])

    svc = _make_service(worker_id="w-b6-1")
    run_id, events = await _run_to_waiting_approval(svc)
    assert events[-1]["status"] == STATUS_WAITING_APPROVAL

    batch1 = await _pending_action_ids(run_id)
    assert len(batch1) == 1, f"批次 1 应为单动作: {batch1}"
    # 第一次恢复：批准批次 1 → 执行后触发批次 2 中断
    events = await _consume(svc.resume_run(
        run_id, [{"type": "approve", "message": "ok", "action_id": batch1[0]}], 1,
    ))
    assert events[-1]["type"] == "done" and events[-1]["status"] == STATUS_WAITING_APPROVAL, \
        f"第一次恢复后应再次进入待审批（第二批）: {events[-3:]}"

    batch2 = await _pending_action_ids(run_id)
    assert len(batch2) == 1 and batch2[0] != batch1[0], f"批次 2 应为新的单动作: {batch2}"
    # 第二次恢复：仅批准批次 2。若把批次 1 决策也带入会数量/位置不齐而失败。
    events = await _consume(svc.resume_run(
        run_id, [{"type": "approve", "message": "ok", "action_id": batch2[0]}], 1,
    ))
    assert events[-1]["type"] == "done" and events[-1]["status"] == STATUS_COMPLETED, \
        f"第二次恢复应完成（决策仅对齐第二批）: {events[-3:]}"
    assert len(calls) == 2, f"两个批次各执行一次工具: {calls}"
    assert [c["args"]["message"] for c in calls] == ["tc-h1", "tc-h2"]


# ── §11-4：多个 action_id 的审批结果严格对应（按位置对齐） ─────────────────

@pytest.mark.asyncio
async def test_t03c_multi_action_batch_strict_mapping(monkeypatch):
    """P1-03 B：一个 Interrupt 批次含多个动作时，决策按 action_id 精确对应。"""
    calls = []
    _setup_hitl_script(monkeypatch, calls, [
        # 同一轮发出两个 webhook_notify → 一个批次两个动作
        AIMessage(content="", tool_calls=[
            {"name": "webhook_notify", "args": {"webhook_url": "u", "message": "A"}, "id": "tc-A"},
            {"name": "webhook_notify", "args": {"webhook_url": "u", "message": "B"}, "id": "tc-B"},
        ]),
        AIMessage(content="完成"),
    ])

    svc = _make_service(worker_id="w-b6-4")
    run_id, events = await _run_to_waiting_approval(svc)

    batch = await _pending_action_ids(run_id)
    assert len(batch) == 2, f"一个批次应含两个动作: {batch}"
    # 严格对应：登记顺序=动作顺序（[A, B]）。A 拒绝、B 批准（按 action_id 对齐）。
    events = await _consume(svc.resume_run(
        run_id, [
            {"type": "reject", "message": "A 不批", "action_id": batch[0]},
            {"type": "approve", "message": "B 放行", "action_id": batch[1]},
        ], 1,
    ))
    assert events[-1]["type"] == "done", f"多动作批次应能恢复: {events[-3:]}"
    # 严格对应：仅 B（批准）执行副作用，A（拒绝）不得执行
    assert [c["args"]["message"] for c in calls] == ["B"], f"仅批准的动作执行: {calls}"


@pytest.mark.asyncio
async def test_t03d_unknown_action_id_rejected():
    """P1-03 B：action_id 必须精确匹配，未知动作显式拒绝（不得按下标猜测）。"""
    svc = _make_service(worker_id="w-b6-b")
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        s.add(AgentApproval(
            run_id=run.id, tool_name="webhook_notify",
            action_id="tc-known", args_hash="h", tool_call_id="tc-known",
            decision_status="pending",
        ))
        await s.commit()
        run_id = run.id
    with pytest.raises(ApprovalInvalid):
        await svc._check_decisions(run_id, [{"type": "approve", "action_id": "no-such"}])


# ── §11-3：重复一致决策幂等 / 冲突决策显式拒绝 / 写入 CAS ───────────────────

@pytest.mark.asyncio
async def test_t03e_idempotent_and_conflict_and_cas():
    """P1-03 C：重复一致决策幂等；冲突决策 409；并发 CAS 只有一个成功。"""
    svc = _make_service(worker_id="w-b6-c")
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        s.add(AgentApproval(
            run_id=run.id, tool_name="webhook_notify",
            action_id="tc-c", args_hash="h", tool_call_id="tc-c",
            decision_status="pending",
        ))
        await s.commit()
        run_id = run.id

    dec = [{"type": "approve", "message": "ok", "action_id": "tc-c"}]
    # 幂等：重复一致决策不报错、不重复写
    assert await svc._check_decisions(run_id, dec) in ("ok", "idempotent")
    await svc._record_approval_decision(run_id, dec, 1)
    assert await svc._check_decisions(run_id, dec) == "idempotent"

    # 冲突：改判显式拒绝
    with pytest.raises(ApprovalConflict):
        await svc._check_decisions(run_id, [{"type": "reject", "action_id": "tc-c"}])

    # CAS：并发两个不同决策写入同一动作，最多一个成功
    import asyncio as _aio
    results = await _aio.gather(
        svc._record_approval_decision(run_id, [{"type": "approve", "action_id": "tc-x"}], 1),
        svc._record_approval_decision(run_id, [{"type": "reject", "action_id": "tc-x"}], 2),
        return_exceptions=True,
    )
    conflicts = sum(1 for r in results if isinstance(r, (ApprovalConflict, ApprovalInvalid)))
    assert conflicts >= 1, f"CAS 并发必须有显式冲突/拒绝: {results}"


# ── §11-6：Checkpoint 丢失不得自动重新推理 ─────────────────────────────────

@pytest.mark.asyncio
async def test_t03f_reconcile_lost_checkpoint_no_auto_reinfer():
    """P1-03 D：Checkpoint 丢失时 confirmed_not_executed 不得自动重新推理。"""
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        s.add(AgentToolExecution(
            run_id=run.id, tool_call_id="tc-r", tool_name="webhook_notify",
            idempotency_key="idem-r", args_hash="h", status=TOOL_EXEC_NEEDS_RECONCILIATION,
        ))
        await s.commit()
        run_id = run.id

    svc = _make_service(worker_id="w-b6-d")
    res = await svc.reconcile_run(
        run_id, tool_call_id="tc-r", disposition="confirmed_not_executed",
        external_operation_id=None, note="未执行", reconciled_by=1,
    )
    # 从不驱动图执行；Checkpoint 缺失时不得自动恢复/重新推理
    assert res["matched"] is True
    assert res["status"] == "prepared"
    assert res["auto_recovery_allowed"] is False, \
        "Checkpoint 丢失时不得自动重新推理（应要求人工重启或新 Run）"


# ── §12-1：API 恒读 DB（Registry 不作状态真相源） ───────────────────────────

@pytest.mark.asyncio
async def test_t12a_get_run_reads_db_not_stale_registry():
    """P1-04：Worker 崩溃后 Registry 可能残留陈旧状态，get_run 必须恒读 DB。"""
    from sqlalchemy import update as _update
    svc = _make_service(worker_id="w-b6-e")
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.execute(_update(AgentRun).where(AgentRun.id == run.id).values(status=STATUS_COMPLETED))
        await s.commit()
        run_id, uid = run.id, run.user_id

    # 人为放入陈旧 Registry 句柄（status 与 DB 不一致）
    RunRegistry.instance().attach(
        run_id=run_id, agent_id=1, session_id=None, user_id=uid,
        thread_id="stale", status=STATUS_RUNNING,
    )
    got = await svc.get_run(run_id, uid)
    assert got["status"] == STATUS_COMPLETED, \
        f"get_run 必须返回 DB 真相（completed），而非 Registry 陈旧 running: {got}"
    RunRegistry.instance()._runs.pop(run_id, None)


# ── §12-4：pending Run 取消与 Worker Claim 互斥 ────────────────────────────

@pytest.mark.asyncio
async def test_t12d_cancel_pending_excludes_claim():
    """P1-04：pending Run 取消后不得被 Worker 领取（取消与 Claim 互斥）。"""
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)                 # 默认 pending
        await s.commit()
        run_id = run.id

        ok = await repo.cancel_without_lease(run_id)     # 取消 pending
        await s.commit()
        assert ok is True

        row = await repo.claim(run_id, "w-x", 30)        # 取消后再领取必须失败
        await s.commit()
        assert row is None, "已取消的 pending Run 不得被 Claim"

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run2, _ = await _create_run(repo)                # 默认 pending
        await s.commit()
        row = await repo.claim(run2.id, "w-y", 30)       # 先领取
        await s.commit()
        assert row is not None
        # 已领取（running）后 cancel_without_lease 必须失败（互斥）
        assert await repo.cancel_without_lease(run2.id) is False


# ── §11-6：needs_reconciliation 三处置生效 + 审计留痕 ─────────────────────

@pytest.mark.asyncio
async def test_t03g_reconcile_three_dispositions_and_audit():
    """P1-03 D：正式核对接口三处置（解决/终止/保持阻断）+ 审计（人/时/外部 operation ID）。"""
    svc = _make_service(worker_id="w-b6-f")
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        for cid in ("tc-ok", "tc-no", "tc-un"):
            s.add(AgentToolExecution(
                run_id=run.id, tool_call_id=cid, tool_name="webhook_notify",
                idempotency_key=f"idem-{cid}", args_hash="h",
                status=TOOL_EXEC_NEEDS_RECONCILIATION,
            ))
        await s.commit()
        run_id = run.id

    # confirmed_succeeded -> succeeded（补录外部 operation ID）
    r1 = await svc.reconcile_run(
        run_id, tool_call_id="tc-ok", disposition="confirmed_succeeded",
        external_operation_id="OP-123", note="下游已成功", reconciled_by=7,
    )
    assert r1["matched"] is True and r1["status"] == TOOL_EXEC_SUCCEEDED

    # confirmed_not_executed -> prepared（允许安全恢复/重试）
    r2 = await svc.reconcile_run(
        run_id, tool_call_id="tc-no", disposition="confirmed_not_executed",
        external_operation_id=None, note="未执行", reconciled_by=7,
    )
    assert r2["matched"] is True and r2["status"] == TOOL_EXEC_PREPARED

    # unresolved -> 保持 needs_reconciliation（阻断，仅留痕）
    r3 = await svc.reconcile_run(
        run_id, tool_call_id="tc-un", disposition="unresolved",
        external_operation_id=None, note="仍无法确认", reconciled_by=7,
    )
    assert r3["matched"] is True and r3["status"] == TOOL_EXEC_NEEDS_RECONCILIATION

    from sqlalchemy import select as _sel
    async with AsyncSessionLocal() as s:
        rows = {r.tool_call_id: r for r in (await s.execute(
            _sel(AgentToolExecution).where(AgentToolExecution.run_id == run_id)
        )).scalars().all()}
    ok = rows["tc-ok"]
    assert ok.status == TOOL_EXEC_SUCCEEDED and ok.external_operation_id == "OP-123"
    assert ok.reconcile_decision == "confirmed_succeeded" and ok.reconciled_by == 7
    assert ok.reconciled_at is not None, "审计须记录核对时间"
    assert rows["tc-no"].status == TOOL_EXEC_PREPARED
    assert rows["tc-no"].reconcile_decision == "confirmed_not_executed"
    assert rows["tc-un"].status == TOOL_EXEC_NEEDS_RECONCILIATION
    assert rows["tc-un"].reconcile_decision == "unresolved"


# ── §12-1：Registry 终态释放 + 容量清理（不随 Run 数线性增长） ──────────────

@pytest.mark.asyncio
async def test_t12b_registry_capacity_and_release_bounded():
    """P1-04 §12：终态 Run 释放 + 满员 _prune，Registry 规模有界。"""
    reg = RunRegistry()          # 独立实例，隔离单例
    reg._max_runs = 50           # 缩小上限便于验证容量清理

    # 终态且 Task 完成 -> release 释放（不得滞留）
    reg.attach(run_id="r-rel", agent_id=1, session_id=None, user_id=1,
               thread_id="t", status=STATUS_COMPLETED)
    reg.release("r-rel")
    assert reg.get("r-rel") is None, "终态 Run 释放后不得滞留 Registry"

    # 塞入大量终态句柄：attach 触发 _prune，规模必须有界（非线性增长）
    for i in range(300):
        reg.attach(run_id=f"r-{i}", agent_id=1, session_id=None, user_id=1,
                   thread_id=f"t{i}", status=STATUS_COMPLETED)
    assert len(reg._runs) <= reg._max_runs, \
        f"Registry 规模必须有界（容量清理）: {len(reg._runs)}"
    assert len(reg._runs) < 100, f"Registry 不得随 Run 数线性增长: {len(reg._runs)}"


# ── §12-3：Worker A 的 Run 可被 Worker B（另一 API 实例）请求取消 ───────────

@pytest.mark.asyncio
async def test_t12c_cross_worker_cancel_requested_visible():
    """P1-04 §12：取消意图写 DB（唯一真相源），跨 Worker 心跳轮询可见。"""
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        lease_v = await repo.claim(run.id, "worker-A", 30)   # Worker A 持有执行权
        await s.commit()
        assert lease_v is not None
        run_id, uid = run.id, run.user_id

    # Worker B（另一 API 实例）：本地 Registry 无该 Run 句柄
    svc_b = _make_service(worker_id="worker-B")
    res = await svc_b.cancel_run(run_id, uid)
    assert res["ok"] is True and res["status"] == "cancelling", \
        f"跨 Worker 取消应写入取消意图并返回 cancelling: {res}"

    # Worker A 侧心跳轮询读 DB 发现取消意图（is_cancel_requested / _cancel_requested）
    async with AsyncSessionLocal() as s:
        assert await RunRepository(s).is_cancel_requested(run_id) is True
    assert await svc_b.worker._cancel_requested(run_id) is True


# ── §12-5：取消在途外部工具不得伪造取消成功（正确记录 unknown） ─────────────

@pytest.mark.asyncio
async def test_t12e_cancel_inflight_external_tool_not_faked_success():
    """P1-04 §12：在途外部工具被取消时不得伪造 succeeded，应记 unknown（禁止重复发起）。"""
    run_id, lease_version = await _claimed_run(worker_id="w-b6-g")
    lease = _lease_for(run_id, lease_version, "w-b6-g")
    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        rec, _ = await repo.begin(
            run_id=run_id, tool_call_id="tc-inflight", tool_name="webhook_notify",
            args_hash="h", idempotency_key="idem-inflight", lease=lease,
        )
        assert await repo.mark_executing(rec.id, lease=lease) is True  # 在途（副作用已发起）
        await s.commit()

        # 取消请求：仅写意图，不得触碰工具账本伪造成功
        await RunRepository(s).request_cancel(run_id, 1)
        await s.commit()
        row = await repo.get(run_id, "tc-inflight")
        assert row.status == TOOL_EXEC_EXECUTING, \
            f"取消不得伪造在途外部工具成功: {row.status}"
        assert row.status != TOOL_EXEC_SUCCEEDED

        # 在途外部工具被中断后正确记录 unknown（结果未知，禁止重复发起）
        assert await repo.mark_unknown(rec.id, error="取消中断，结果未知", lease=lease) is True
        await s.commit()
        row = await repo.get(run_id, "tc-inflight")
        assert row.status == TOOL_EXEC_UNKNOWN
        assert row.status != TOOL_EXEC_SUCCEEDED, "不得伪造取消成功"


@pytest.mark.asyncio
async def test_event_overflow_keeps_persisted_and_redacts_payload():
    """内存队列溢出不丢持久化事件；payload 入库前脱敏（FR-07）。"""
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
    run_id = run.id
    bus = EventBus(maxsize=1, persist=True)  # 极小队列强制溢出
    bus.bind_run(thread_id="t", agent_id=1)
    for i in range(5):
        await bus.publish(RuntimeEvent(
            type=MODEL_DELTA,
            run_id=run_id,
            data={"text": f"t{i}", "api_key": "sk-secret", "index": i},
        ))
    # P1-06 §14.5：MODEL_DELTA 走批量缓冲，收尾 flush 后才保证全部落库
    # （与 _consume_stream 收尾行为一致；断言语义不变：溢出仅丢通知、DB 全量、入库脱敏）
    await bus.flush_deltas()
    async with AsyncSessionLocal() as s:
        rows = await EventRepository(s).list_after(run_id, 0)
    assert [r.sequence for r in rows] == [1, 2, 3, 4, 5]  # 溢出仅丢通知，DB 全量
    assert rows[0].payload["api_key"] == "***"            # 入库前已脱敏
    assert rows[0].payload["text"] == "t0"


@pytest.mark.asyncio
async def test_event_sequence_concurrent_appends_unique():
    """并发追加下 run 内事件序号原子唯一（sequence 数据库并发安全）。"""
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
    run_id = run.id

    async def one(i: int) -> int:
        async with AsyncSessionLocal() as s:
            seq = await EventRepository(s).append(run_id, "model_delta", {"i": i})
            await s.commit()
        return seq

    seqs = await asyncio.gather(*[one(i) for i in range(12)])
    assert sorted(seqs) == list(range(1, 13))


@pytest.mark.asyncio
async def test_event_retention_cleanup_wired_in_worker():
    """保留策略接线：Worker 清理终态超期 Run 事件，运行中 Run 一律保留（FR-07）。"""
    svc = _make_service(worker_id="w-cleanup")

    async with AsyncSessionLocal() as s:
        runs = RunRepository(s)
        old_run, _ = await _create_run(runs)   # 终态但超期
        await runs.claim(old_run.id, "w", 30)
        await runs.update_status(
            old_run.id, status=STATUS_COMPLETED,
            lease=RunExecutionLease(run_id=old_run.id, worker_id="w", lease_version=1),
            finished=True,
        )
        live_run, _ = await _create_run(runs)  # 仍在运行
        live_run.status = STATUS_WAITING_APPROVAL
        old_row = await runs.get_run(old_run.id)
        old_row.finished_at = old_row.finished_at.replace(year=2000)
        events = EventRepository(s)
        await events.append(old_run.id, "run_completed", {})
        await events.append(live_run.id, "run_started", {})
        await s.commit()

    await svc.worker._cleanup_events()

    async with AsyncSessionLocal() as s:
        events = EventRepository(s)
        assert await events.list_after(old_run.id, 0) == []       # 超期终态已清理
        assert len(await events.list_after(live_run.id, 0)) == 1  # 运行中保留


# ── Phase 6：消息去重 / 最终文本竞态 / 模型计数（T-09 / T-10 / T-11） ──


def _plain_agent_config(tools: List[str] = None):
    return {
        "id": 1,
        "model_config_id": 1,
        "tools": list(tools or []),
        "system_prompt": "测试",
    }


class _ChunkedChatModel(BaseChatModel):
    """一次模型请求产出多个可见文本 Chunk（模拟流式 token）。

    流式 Chunk 与最终消息共用消息 ID，LangGraph messages 管道按 ID 去重，
    最终消息不重复下发 —— 多 Chunk 仍是一次模型请求。
    """

    chunks: List[str] = []

    @property
    def _llm_type(self) -> str:
        return "fake-chunked"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        for c in self.chunks:
            if run_manager:
                run_manager.on_llm_new_token(
                    c,
                    chunk=ChatGenerationChunk(message=AIMessageChunk(content=c, id="m1")),
                )
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content="".join(self.chunks), id="m1"),
        )])


@pytest.mark.asyncio
async def test_t09_multi_turn_history_no_duplication_after_restart(monkeypatch):
    """T-09（FR-08）：多轮会话消息去重——每条用户/助手消息在 Graph state 中
    只出现一次；服务重启后继续多轮仍正确；Session 每轮恰好写回一条 assistant。

    每轮注入完整会话历史（模拟 /chat 行为），轮与轮之间清空内存注册表模拟
    进程重启，仅数据库 + Checkpoint 保留；末尾以同一历史重试注入验证
    重试语义不产生重复。
    """
    import app.services.agent_runtime.factory as factory_mod
    from app.services.agent_runtime.persistence import make_thread_id

    answers = ["回答一", "回答二", "回答三", "回答四"]
    model = _FakeChatModel(script=[AIMessage(content=a) for a in answers])
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)
    monkeypatch.setattr(AgentRuntimeService, "_load_model", _fake_load_model)
    monkeypatch.setattr(AgentRuntimeService, "_load_agent_config", _fake_load_agent_config)

    async with AsyncSessionLocal() as s:
        sess = AgentSession(agent_id=1, user_id=1, messages=[])
        s.add(sess)
        await s.commit()
        sid = sess.id

    history: List[dict] = []
    for i in range(3):
        history.append({"role": "user", "content": f"消息{i + 1}"})
        # FR-08.1：用户消息在 Run 创建前可靠持久化（模拟 /chat 预存）
        async with AsyncSessionLocal() as s:
            from sqlalchemy.orm.attributes import flag_modified

            row = (await s.execute(select(AgentSession).where(AgentSession.id == sid))).scalar_one()
            row.messages = list(history)
            flag_modified(row, "messages")
            await s.commit()
        # 模拟服务重启：内存句柄/注册表清空，仅剩数据库 + Checkpoint
        RunRegistry.instance()._runs.clear()
        svc = _make_service(worker_id=f"w-t9-{i}")
        run_id, stream = await svc.start_run(
            agent_config=_plain_agent_config(),
            messages=list(history),
            user_id=1,
            session_id=sid,
        )
        events = [e async for e in stream]
        await svc.worker.stop()
        assert events[-1]["type"] == "done" and events[-1]["status"] == STATUS_COMPLETED
        history.append({"role": "assistant", "content": answers[i]})

    # 用同一 Checkpoint 构建等价图，读取 Graph state（消息通道）验证去重
    from app.services.agent_runtime.persistence import CheckpointerFactory

    cp = await CheckpointerFactory.get().get_checkpointer()
    provider, model_config = await _fake_load_model(None, 1)
    graph = factory_mod.build_agent(
        _make_ctx("t9-state"),
        ToolPolicy.from_settings(set(), enable_hitl=False),
        EventBus(),
        provider,
        model_config,
        [],
        checkpointer=cp,
    )
    thread_cfg = {"configurable": {"thread_id": make_thread_id(1, sid, "")}}

    state = await graph.aget_state(thread_cfg)
    contents = [m.content for m in state.values.get("messages", [])]
    for expect in ["消息1", "回答一", "消息2", "回答二", "消息3", "回答三"]:
        assert contents.count(expect) == 1, f"{expect} 在 state 中出现 {contents.count(expect)} 次"

    # Session 写回（FR-08.2）：每轮恰好一条 assistant，顺序与内容正确
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(AgentSession).where(AgentSession.id == sid))
        msgs = res.scalar_one().messages
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3
    assert [m["content"] for m in msgs if m["role"] == "assistant"] == answers[:3]

    # 同一历史重试注入（FR-08 重试语义）：state 不产生重复追加
    RunRegistry.instance()._runs.clear()
    svc = _make_service(worker_id="w-t9-retry")
    run_id, stream = await svc.start_run(
        agent_config=_plain_agent_config(),
        messages=list(history),
        user_id=1,
        session_id=sid,
    )
    events = [e async for e in stream]
    await svc.worker.stop()
    assert events[-1]["status"] == STATUS_COMPLETED

    state = await graph.aget_state(thread_cfg)
    contents = [m.content for m in state.values.get("messages", [])]
    for expect in ["消息1", "回答一", "消息2", "回答二", "消息3", "回答三", "回答四"]:
        assert contents.count(expect) == 1, f"重试后 {expect} 出现 {contents.count(expect)} 次"


@pytest.mark.asyncio
async def test_t10_final_text_authoritative_without_consumer(monkeypatch):
    """T-10（FR-09）：事件消费者延迟/断线下，_drive 完成时 run.final_text 已完整；
    非流式响应与数据库最终结果一致；LegacyEventAdapter 不承担唯一结果收集职责。

    运行全程不消费事件流（模拟 SSE 消费者断线），完成后才消费并与 DB 对账。
    """
    import app.services.agent_runtime.factory as factory_mod

    model = _ChunkedChatModel(chunks=["答案甲-", "答案乙-", "答案丙"])
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)
    monkeypatch.setattr(AgentRuntimeService, "_load_model", _fake_load_model)
    monkeypatch.setattr(AgentRuntimeService, "_load_agent_config", _fake_load_agent_config)
    full = "答案甲-答案乙-答案丙"

    svc = _make_service(worker_id="w-t10-1")
    run_id, stream = await svc.start_run(
        agent_config=_plain_agent_config(),
        messages=[{"role": "user", "content": "hi"}],
        user_id=1,
        session_id=None,
    )
    # 不消费 stream：消费者延迟/断线；_drive 独立完成执行与结果持久化
    row = await _wait_status(run_id, {STATUS_COMPLETED})
    assert row is not None and row.status == STATUS_COMPLETED
    assert row.final_text == full, "_drive 完成时最终文本必须已完整（不依赖消费者）"
    assert row.summary["model_calls"] == 1  # 3 个 Chunk = 1 次模型请求

    # 消费者晚到：旧契约 final 全文与 DB 权威结果一致
    events = [e async for e in stream]
    finals = [e for e in events if e["type"] == "final"]
    assert finals and finals[-1]["content"] == row.final_text
    await svc.worker.stop()

    # 非流式 run_to_completion 与数据库最终结果一致（同一权威收集）
    svc2 = _make_service(worker_id="w-t10-2")
    result = await svc2.run_to_completion(
        agent_config=_plain_agent_config(),
        messages=[{"role": "user", "content": "hi"}],
        user_id=1,
        session_id=None,
    )
    row2 = await _wait_status(result["run_id"], {STATUS_COMPLETED})
    assert result["content"] == row2.final_text == full
    await svc2.worker.stop()


@pytest.mark.asyncio
async def test_t11_model_call_counting_and_budget_limit(monkeypatch):
    """T-11（FR-10）：多 Chunk 一次模型请求只计一次；tool_calls 口径准确；
    预算耗尽 → limit_exceeded（不是 failed）。"""
    import app.services.agent_runtime.factory as factory_mod

    monkeypatch.setattr(AgentRuntimeService, "_load_model", _fake_load_model)
    monkeypatch.setattr(AgentRuntimeService, "_load_agent_config", _fake_load_agent_config)

    # (a) 多 Chunk 单请求：请求 1 次、生成 Token 3、model 节点 1 次
    model = _ChunkedChatModel(chunks=["甲", "乙", "丙"])
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)
    svc = _make_service(worker_id="w-t11-a")
    result = await svc.run_to_completion(
        agent_config=_plain_agent_config(),
        messages=[{"role": "user", "content": "hi"}],
        user_id=1,
        session_id=None,
    )
    row = await _wait_status(result["run_id"], {STATUS_COMPLETED})
    assert result["content"] == "甲乙丙"
    assert row.summary["model_calls"] == 1, "多 Chunk 不得按 Chunk 计数"
    assert row.summary["tokens_generated"] == 3
    assert row.summary["model_node_runs"] == 1 and row.summary["steps"] == 1
    await svc.worker.stop()

    # (b) 工具轮次：模型请求 2 次、工具调用 1 次（真实 calculator 工具）
    model2 = _FakeChatModel(script=[
        AIMessage(content="", tool_calls=[{
            "name": "calculator", "args": {"expression": "21*2"}, "id": "tc-c1",
        }]),
        AIMessage(content="结果42"),
    ])
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model2)
    svc2 = _make_service(worker_id="w-t11-b")
    result2 = await svc2.run_to_completion(
        agent_config=_plain_agent_config(tools=["calculator"]),
        messages=[{"role": "user", "content": "算 21*2"}],
        user_id=1,
        session_id=None,
    )
    row2 = await _wait_status(result2["run_id"], {STATUS_COMPLETED})
    assert row2.summary["model_calls"] == 2
    assert row2.summary["tool_calls"] == 1
    assert row2.summary["model_node_runs"] == 2
    await svc2.worker.stop()

    # (c) 预算耗尽：max_model_calls=1 + 两次请求 -> limit_exceeded
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "AGENT_MAX_MODEL_CALLS", 1)
    model3 = _FakeChatModel(script=[
        AIMessage(content="", tool_calls=[{
            "name": "calculator", "args": {"expression": "1+1"}, "id": "tc-c2",
        }]),
        AIMessage(content="2"),
    ])
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model3)
    svc3 = _make_service(worker_id="w-t11-c")
    result3 = await svc3.run_to_completion(
        agent_config=_plain_agent_config(tools=["calculator"]),
        messages=[{"role": "user", "content": "算 1+1"}],
        user_id=1,
        session_id=None,
    )
    assert result3["status"] == STATUS_LIMIT_EXCEEDED
    row3 = await _wait_status(result3["run_id"], {STATUS_LIMIT_EXCEEDED})
    assert row3 is not None and row3.status == STATUS_LIMIT_EXCEEDED
    await svc3.worker.stop()


# ════════════════════════════════════════════════════════════════════════════
# Phase 2.1 §13 P1-05：Run 级执行预算持久化（跨崩溃 / HITL 恢复不重置）
# ════════════════════════════════════════════════════════════════════════════


async def _budget_seed(worker_id: str, **advance):
    """创建 + claim 一个 Run，返回 (run_id, lease_version, ensure_budget 快照)。"""
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, worker_id, 30)
        state = await repo.ensure_budget(run.id, timeout_seconds=300.0)
        if advance:
            await repo.advance_budget(run.id, expected_version=state["version"], **advance)
            state = await repo.ensure_budget(run.id, timeout_seconds=300.0)
        await s.commit()
    return run.id, version, state


def _budget_ctx(run_id: str, **kw):
    from app.services.agent_runtime.context import ExecutionBudget
    return RuntimeContext(
        scope=RunScope(user_id=1), run_id=run_id, thread_id=f"t-{run_id[:6]}",
        agent_id=1, budget=ExecutionBudget(run_timeout_seconds=300.0, **kw),
    )


def _budget_handle(run_id: str):
    return RunHandle(
        run_id=run_id, agent_id=1, session_id=None, user_id=1,
        thread_id=f"t-{run_id[:6]}",
    )


def _dt_eq(a, b, tol: float = 1.0) -> bool:
    """跨库 datetime 秒级等值比较（P1-05 验收断言）。

    MySQL 的 DATETIME(0) 列会把小数秒四舍五入到整秒，而 ensure_budget 返回的
    是带微秒的 Python 计算值，二者直接 == 会因存储精度不同而假失败。这里以 1 秒
    容差吸收 DB 存储精度差异；被测语义（deadline 未顺延/未重置，差异达 120s/300s）
    仍被完整检出——任何真实重置/顺延都会远超容差。
    """
    return abs((a - b).total_seconds()) < tol


@pytest.mark.asyncio
async def test_p15_budget_continues_from_10_after_restart():
    """§13 验收1：Run 执行 10 次模型调用后重启，恢复后预算从 10 继续（非从 0）。"""
    run_id, _lease, state = await _budget_seed("w-p15-1", model_calls=10, steps=10, tokens=100)
    assert state["model_calls"] == 10

    # 模拟 Worker 重启：内存 ctx 全新构造（计数器归零），仅靠 DB 累计恢复
    svc = _make_service(worker_id="w-p15-1b")
    ctx = _budget_ctx(run_id)
    assert ctx.model_calls == 0, "重启后新 ctx 内存计数器确实从零开始"
    await svc._load_budget(ctx, _budget_handle(run_id))

    # 恢复后预算从 10 继续，而非从 0
    assert ctx.model_calls == 10, f"恢复后模型调用预算应从 10 继续，实得 {ctx.model_calls}"
    assert ctx.steps == 10 and ctx.tokens_generated == 100
    # 已刷快照 = 历史累计，本轮 flush 仅累计增量（不重复计数）
    assert ctx._flushed_model_calls == 10 and ctx._flushed_tokens == 100


@pytest.mark.asyncio
async def test_p15_worker_restart_cannot_bypass_max_tool_calls():
    """§13 验收2：Worker 多次重启不能绕过最大工具调用次数（累计不被重置）。"""
    from app.core.config import settings as app_settings

    run_id, _lease, _ = await _budget_seed("w-p15-2")
    handle = _budget_handle(run_id)
    max_tool = 4

    # 多次"Worker 重启"：每次新 ctx、_load_budget 从 DB 累计续算，逐次逼近上限
    for cycle in range(6):
        svc = _make_service(worker_id=f"w-p15-2-{cycle}")
        ctx = _budget_ctx(run_id, max_tool_calls=max_tool)
        await svc._load_budget(ctx, handle)
        # 模拟本轮又执行一次工具调用 -> ctx.tool_calls 递增 -> flush 累计
        ctx.tool_calls += 1
        await svc._flush_budget(ctx, handle)

    async with AsyncSessionLocal() as s:
        state = await RunRepository(s).ensure_budget(run_id, timeout_seconds=300.0)
    # 6 次重启累计 6 次工具调用，没有一次被"从零开始"绕过
    assert state["tool_calls"] == 6, f"跨重启工具调用须累计，实得 {state['tool_calls']}"
    assert state["tool_calls"] > max_tool, "累计必须能超过单次预算上限（不被重置绕过）"

    # 预算口径一致性：exhausted_reason 依据累计值判超限
    from app.services.agent_runtime.context import ExecutionBudget
    budget = ExecutionBudget(max_tool_calls=max_tool)
    assert budget.exhausted_reason(0, state["tool_calls"], 0) is not None, \
        "累计工具调用超限必须被识别（重启不得绕过上限）"


@pytest.mark.asyncio
async def test_p15_hitl_wait_timeout_is_explicitly_configurable(monkeypatch):
    """§13 验收3：HITL 等待时间是否计入 Run Timeout 由配置显式决定。

    - AGENT_HITL_WAIT_COUNTS_TO_TIMEOUT=True：等待计入超时，deadline 不顺延；
    - =False：等待不计入超时，恢复时按等待时长顺延 deadline。
    """
    from datetime import datetime, timedelta

    from app.core.config import settings as app_settings

    run_id, _lease, state = await _budget_seed("w-p15-3")
    base_deadline = state["deadline_at"]
    assert base_deadline is not None

    svc = _make_service(worker_id="w-p15-3")
    # 模拟已进入 waiting_approval 一段时长（等待起点设为过去）
    async with AsyncSessionLocal() as s:
        await RunRepository(s).clear_hitl_wait(run_id)  # 先清零
        await s.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(
                hitl_wait_started_at=datetime.utcnow() - timedelta(seconds=120)
            )
        )
        await s.commit()

    # (a) True：等待计入超时 -> deadline 不变
    monkeypatch.setattr(app_settings, "AGENT_HITL_WAIT_COUNTS_TO_TIMEOUT", True)
    await svc._apply_hitl_wait_policy(run_id)
    async with AsyncSessionLocal() as s:
        after_true = (await RunRepository(s).get_run(run_id)).budget_deadline_at
    assert _dt_eq(after_true, base_deadline), "等待计入超时时 deadline 不得顺延"

    # 重新置等待起点
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(
                hitl_wait_started_at=datetime.utcnow() - timedelta(seconds=120)
            )
        )
        await s.commit()

    # (b) False：等待不计入超时 -> deadline 按等待时长顺延（约 +120s）
    monkeypatch.setattr(app_settings, "AGENT_HITL_WAIT_COUNTS_TO_TIMEOUT", False)
    await svc._apply_hitl_wait_policy(run_id)
    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    shifted = (row.budget_deadline_at - base_deadline).total_seconds()
    assert 100 <= shifted <= 140, f"等待不计入超时时应顺延 deadline 约 120s，实得 {shifted}s"
    assert row.hitl_wait_started_at is None, "顺延后应清除等待起点（避免重复顺延）"


@pytest.mark.asyncio
async def test_p15_deadline_absolute_not_reset_on_restart():
    """§13 验收4：deadline 到期后即使重启也不能重新获得完整时间（绝对时间）。"""
    from datetime import datetime, timedelta

    run_id, _lease, state = await _budget_seed("w-p15-4")
    d1 = state["deadline_at"]

    # 模拟重启后再次 ensure_budget：deadline 保持原绝对值，不重设为 now+timeout
    await asyncio.sleep(0.01)
    async with AsyncSessionLocal() as s:
        again = await RunRepository(s).ensure_budget(run_id, timeout_seconds=300.0)
    assert _dt_eq(again["deadline_at"], d1), "重启后 deadline 须保持绝对值，不得重新获得完整超时"

    # deadline 已过期后重启：不得重置为 now+timeout（仍指向过去）
    past = datetime.utcnow() - timedelta(seconds=5)
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(budget_deadline_at=past)
        )
        await s.commit()
    svc = _make_service(worker_id="w-p15-4b")
    ctx = _budget_ctx(run_id)
    await svc._load_budget(ctx, _budget_handle(run_id))
    assert _dt_eq(ctx.budget_deadline_at, past), "过期 deadline 不得被重启刷新为完整超时"
    # 剩余时间按绝对 deadline 计算（已过期 -> 0），不重新计满
    remaining = (ctx.budget_deadline_at - datetime.utcnow()).total_seconds()
    assert remaining <= 0, f"过期 deadline 恢复后不得重新获得完整时间，剩余 {remaining}s"


@pytest.mark.asyncio
async def test_p15_subagent_budget_shares_parent_ctx():
    """§13 验收5：多 Agent 子任务预算口径一致（子 Agent 工具计入父 Run ctx 预算）。"""
    from app.services.agent_runtime.factory import _subagent_tools
    from app.services.agent_runtime.tools.policy import ToolPolicy

    run_id, lease_version, _ = await _budget_seed("w-p15-5")

    # 父运行 ctx（唯一预算池）：子 Agent 工具经同一 adapter 构建 -> 共享父 ctx
    ctx = _budget_ctx(run_id)
    ctx.worker_id = "w-p15-5"
    ctx.lease_version = lease_version
    bus = EventBus(persist=True)
    bus.bind_run(thread_id=f"t-{run_id[:6]}", agent_id=1, trace_id=run_id)
    adapter = ToolAdapterFactory(ctx, ToolPolicy.from_settings({"calculator"}, enable_hitl=False), bus)

    # 子 Agent 工具集由父 adapter 构建（收窄交集），共享同一 ctx 预算池
    sub_tools = _subagent_tools({"name": "data-analyst", "tools": ["calculator"]}, adapter)
    assert sub_tools, "子 Agent 工具集非空"
    assert all(t.name in ("calculator",) for t in sub_tools), "子 Agent 工具须按策略收窄"

    # 执行体替身（真实账本 + 可注入行为）
    calls = []

    async def behavior(name, args):
        calls.append(name)
        return "3"

    monkeypatch_executor = _fake_executor(behavior)
    import app.services.agent_runtime.tools.adapter as adapter_mod
    orig = adapter_mod.ToolExecutor
    adapter_mod.ToolExecutor = monkeypatch_executor
    try:
        tool = next(t for t in sub_tools if t.name == "calculator")
        # 含 InjectedToolCallId 的工具须以完整 ToolCall 形式调用
        await tool.ainvoke({
            "name": "calculator", "type": "tool_call", "id": "tc-sub1",
            "args": {"expression": "1+2"},
        })
    finally:
        adapter_mod.ToolExecutor = orig

    # 子 Agent 工具调用计入父 ctx.tool_calls（同一预算池，口径一致）
    assert ctx.tool_calls == 1, f"子 Agent 工具调用须计入父 Run 预算，实得 {ctx.tool_calls}"
    assert calls == ["calculator"]
    assert ctx.run_id == run_id, "预算归属父 Run（子任务不另开预算池）"

    # flush 到 DB：累计进父 Run budget_tool_calls（口径一致、不重复计）
    svc = _make_service(worker_id="w-p15-5b")
    await svc._flush_budget(ctx, _budget_handle(run_id))
    async with AsyncSessionLocal() as s:
        state = await RunRepository(s).ensure_budget(run_id, timeout_seconds=300.0)
    assert state["tool_calls"] == 1, f"子任务工具调用须累计进父 Run 预算，实得 {state['tool_calls']}"


# ════════════════════════════════════════════════════════════════════════════
# Phase 2.1 §14 P1-06：事件订阅、回放与性能（序号权威源 / Last-Event-ID / 批量）
# ════════════════════════════════════════════════════════════════════════════


async def _p16_seed_events(worker_id: str, deltas: int, *, batch_size: int = 32,
                           terminal: bool = True):
    """建 Run + 经真实 EventBus 持久化一组 MODEL_DELTA + 终态事件。"""
    from app.services.agent_runtime.events import EventBus, RuntimeEvent, MODEL_DELTA, RUN_COMPLETED

    run_id, _lease, _ = await _budget_seed(worker_id)
    bus = EventBus(persist=True, delta_batch_size=batch_size)
    bus.bind_run(thread_id=f"t-{run_id[:6]}", agent_id=1, trace_id=run_id)
    parts = []
    for i in range(deltas):
        text = f"part{i} "
        parts.append(text)
        await bus.publish(RuntimeEvent(type=MODEL_DELTA, run_id=run_id, data={"text": text}))
    if terminal:
        await bus.publish(RuntimeEvent(
            type=RUN_COMPLETED, run_id=run_id, data={"final_text": "".join(parts)}
        ))
    await bus.flush_deltas()
    return run_id, "".join(parts)


async def _p16_consume_all(svc, run_id):
    """订阅者经 replay_events 从 DB 事实日志读取全部可见事件（到 done 为止）。"""
    got = []
    async for ev in svc.replay_events(run_id):
        got.append(ev)
        if ev.get("type") == "done":
            break
    return got


@pytest.mark.asyncio
async def test_p16_two_subscribers_both_get_complete_events():
    """§14 验收1：同一 Run 两个 SSE 订阅者都能收到完整事件。"""
    run_id, _ = await _p16_seed_events("w-p16-1", deltas=6)

    svc_a = _make_service(worker_id="w-p16-1a")
    svc_b = _make_service(worker_id="w-p16-1b")
    sub_a, sub_b = await asyncio.gather(
        _p16_consume_all(svc_a, run_id), _p16_consume_all(svc_b, run_id)
    )
    types_a = [e.get("type") for e in sub_a]
    types_b = [e.get("type") for e in sub_b]
    # 两个订阅者各自读到完整事件序列（不因竞争共享队列而缺漏）
    # 注：MODEL_DELTA 在旧 SSE 契约里映射为 final_chunk（流式文本增量）
    assert types_a == types_b, "两个订阅者应收到一致的完整事件序列"
    assert types_a.count("final_chunk") == 6, f"应各收到 6 个增量，实得 {types_a.count('final_chunk')}"
    assert types_a[-1] == "done" and "final" in types_a


@pytest.mark.asyncio
async def test_p16_queue_overflow_subscriber_catches_up_from_db():
    """§14 验收2：事件队列溢出后订阅者能自动补齐（通知丢失，DB 事实日志完整）。"""
    from app.services.agent_runtime.events import EventBus, RuntimeEvent, MODEL_DELTA, RUN_COMPLETED

    run_id, _lease, _ = await _budget_seed("w-p16-2")
    # 极小队列制造溢出：内存通知被丢弃，但持久化事件仍在 DB
    bus = EventBus(persist=True, maxsize=2)
    bus.bind_run(thread_id=f"t-{run_id[:6]}", agent_id=1, trace_id=run_id)
    for i in range(10):
        await bus.publish(RuntimeEvent(type=MODEL_DELTA, run_id=run_id, data={"text": f"t{i}"}))
    await bus.publish(RuntimeEvent(type=RUN_COMPLETED, run_id=run_id, data={"final_text": "done"}))
    await bus.flush_deltas()

    # 订阅者不依赖内存队列，从 DB 事实日志自动补齐全部事件
    svc = _make_service(worker_id="w-p16-2s")
    got = await _p16_consume_all(svc, run_id)
    deltas = [e.get("type") for e in got].count("final_chunk")
    assert deltas == 10, f"队列溢出后应从 DB 补齐全部 10 个增量，实得 {deltas}"
    assert got[-1].get("type") == "done"


@pytest.mark.asyncio
async def test_p16_last_event_id_resume_no_dup_no_gap():
    """§14 验收3：Last-Event-ID 可精确续传，无重复、无漏事件。"""
    run_id, _ = await _p16_seed_events("w-p16-3", deltas=8)

    svc = _make_service(worker_id="w-p16-3")
    # 第一次订阅：取前 3 个事件后断开
    first = []
    async for ev in svc.replay_events(run_id):
        first.append(ev)
        if len(first) >= 3:
            break
    last_seq = first[-1].get("seq")
    assert last_seq is not None

    # 从 Last-Event-ID（last_seq）之后精确续传
    resumed = await _p16_consume_all(svc, run_id)  # 全量（含 first）
    after = [e for e in resumed if e.get("seq") > last_seq]
    # 无重复：续传事件序号均 > last_seq
    assert all(e.get("seq") > last_seq for e in after), "续传不得重复已收事件"
    # 无漏 + 无重复：first ∪ after = 全部事件，序号严格连续递增
    merged = sorted(first + after, key=lambda e: e.get("seq"))
    all_seqs = [e.get("seq") for e in merged]
    assert all_seqs == sorted(set(all_seqs)), "事件序号不得重复"
    # 严格连续（无缺口）：相邻序号差 1
    for a, b in zip(all_seqs, all_seqs[1:]):
        assert b - a == 1, f"事件序号须连续无缺口：{all_seqs}"


@pytest.mark.asyncio
async def test_p16_cross_process_subscription_reads_db_not_queue():
    """§14 验收4：跨进程订阅依赖数据库事实日志，不依赖进程内 Queue。"""
    run_id, _ = await _p16_seed_events("w-p16-4", deltas=5)

    # 进程 B：全新服务实例、全新 adapter、不接触进程 A 的 EventBus 队列
    svc_b = _make_service(worker_id="w-p16-4-cross")
    got = await _p16_consume_all(svc_b, run_id)
    # 完全从 DB 事实日志读到全部事件（零依赖进程内内存队列）
    assert [e.get("type") for e in got].count("final_chunk") == 5
    assert got[-1].get("type") == "done"


@pytest.mark.asyncio
async def test_p16_model_delta_batching_reduces_db_transactions(monkeypatch):
    """§14 验收5：100 Token/s 的 MODEL_DELTA 不产生 100 次/s 独立数据库事务。"""
    import app.core.database as db_mod
    from app.services.agent_runtime.events import EventBus, RuntimeEvent, MODEL_DELTA, RUN_COMPLETED

    run_id, _lease, _ = await _budget_seed("w-p16-5")  # 建 run（不计入事务计数）

    bus = EventBus(persist=True, delta_batch_size=32)
    bus.bind_run(thread_id=f"t-{run_id[:6]}", agent_id=1, trace_id=run_id)

    # 计数真实数据库事务：每次 AsyncSessionLocal() 开启 = 一个独立事务
    txn = {"n": 0}
    orig = db_mod.AsyncSessionLocal

    def counting():
        txn["n"] += 1
        return orig()

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", counting)

    # 100 个 MODEL_DELTA（模拟 100 Token/s）+ 1 个终态
    for i in range(100):
        await bus.publish(RuntimeEvent(type=MODEL_DELTA, run_id=run_id, data={"text": f"t{i}"}))
    await bus.publish(RuntimeEvent(type=RUN_COMPLETED, run_id=run_id, data={"final_text": "done"}))
    await bus.flush_deltas()

    # 批量持久化：100 个增量绝不产生 100 个事务
    assert txn["n"] < 20, f"100 Token 增量产生 {txn['n']} 个独立数据库事务，应远小于 100（批量持久化）"


@pytest.mark.asyncio
async def test_p16_final_text_complete_after_batching():
    """§14 验收6：事件批量化后最终文本恢复仍然完整。"""
    run_id, full_text = await _p16_seed_events("w-p16-6", deltas=10, batch_size=4)

    # 多批批量持久化后，从 DB 重建最终文本完整无缺
    svc = _make_service(worker_id="w-p16-6")
    rebuilt = await svc._load_prior_final_text(run_id)
    assert "".join(rebuilt) == full_text, "批量持久化后最终文本重建须完整（不丢 Token）"
    assert len(rebuilt) == 10, f"每 Token 仍是一条独立事件（不合并行），实得 {len(rebuilt)}"

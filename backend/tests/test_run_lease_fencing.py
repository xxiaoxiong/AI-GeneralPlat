"""P0-02 Run Lease / Heartbeat / Fencing / 数据库状态机闭环（Batch 2 验收）。

覆盖任务书 LF-01～LF-07 场景 + 数据库级状态机闭环 + 多 Worker 真实独立进程
并发（不仅仅是同一事件循环内的 asyncio Task）+ 优雅停机 graceful drain。

数据库时间说明：租约比较在 SQL 中统一走数据库时间（sqlite: CURRENT_TIMESTAMP /
datetime('now', ...)，UTC），测试中模拟"租约过期"直接把 lease_expires_at
改写为过去时间，与多机时钟偏差场景等价。
"""
import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, update

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import (
    Agent,
    AgentApproval,
    AgentRun,
    AgentRunEvent,
    AgentToolExecution,
)
from app.services.agent_runtime.events import LegacyEventAdapter
from app.services.agent_runtime.lifecycle import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    RunExecutionLease,
)
from app.services.agent_runtime.repositories import (
    HEARTBEAT_LOST,
    EventRepository,
    RunRepository,
    ToolExecutionRepository,
)
from app.services.agent_runtime.repositories.errors import (
    IllegalTransition,
    RunLeaseLost,
)
from app.services.agent_runtime.repositories.tool_execution_repository import (
    TOOL_EXEC_PREPARED,
)
from app.services.agent_runtime.service import AgentRuntimeService
from app.services.agent_runtime.worker import RunJob, RunWorker


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in (AgentRunEvent, AgentToolExecution, AgentApproval, AgentRun):
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


def _uid() -> str:
    return uuid.uuid4().hex


async def _create_run(repo: RunRepository, **kw):
    run_id = _uid()
    # 真实 PG/MySQL 有外键约束：先确保 agent 行存在；user_id 保持 NULL（无 users 行）
    agent = await repo.session.get(Agent, 1)
    if agent is None:
        agent = Agent(id=1, name="lf-agent")
        repo.session.add(agent)
        await repo.session.flush()
    defaults = dict(
        run_id=run_id,
        agent_id=1,
        session_id=None,
        user_id=None,
        thread_id=f"agent-1-session-{run_id[:8]}",
    )
    defaults.update(kw)
    return await repo.create_run(**defaults)


def _make_service(*, worker_id="w-fence", lease_seconds=30, shutdown_grace=5.0):
    svc = AgentRuntimeService()
    svc.worker = RunWorker(
        svc,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        shutdown_grace=shutdown_grace,
    )
    return svc


async def _expire_lease(run_id: str) -> None:
    """把租约改写为过期时间（等价于租约到期未续、无新 Worker 接管）。"""
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(lease_expires_at=datetime(2000, 1, 1))
        )
        await s.commit()


async def _steal_lease(run_id: str, new_worker: str) -> int:
    """模拟新 Worker 接管：递增 lease_version 并易主，返回新版本。"""
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(worker_id=new_worker, lease_version=AgentRun.lease_version + 1)
        )
        await s.commit()
        row = await RunRepository(s).get_run(run_id)
        return row.lease_version


# ════════════════════════════════════════════════════════════════════════════
# LF-01：Lease 到期但没有新 Worker Claim → 旧 Worker 不能续租
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lf01_expired_lease_cannot_self_renew():
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
        v1 = await RunRepository(s).claim(run_id, "worker-a", 30)
        await s.commit()
    assert v1 is not None

    await _expire_lease(run_id)

    async with AsyncSessionLocal() as s:
        result = await RunRepository(s).heartbeat(run_id, "worker-a", v1, 30)
        await s.commit()
        assert result == HEARTBEAT_LOST, "LF-01：租约过期不得自行续租（即使无人接管）"

        row = await RunRepository(s).get_run(run_id)
        assert row.lease_expires_at < datetime.utcnow(), "租约未被私自恢复"
        assert row.status == STATUS_RUNNING
        assert row.worker_id == "worker-a"

        # 必须重新走原子 Claim 才能恢复执行权（attempt/lease_version 递增）
        v2 = await RunRepository(s).claim(run_id, "worker-a", 30)
        await s.commit()
        assert v2 == v1 + 1
        row = await RunRepository(s).get_run(run_id)
        assert row.lease_expires_at > datetime.utcnow()


# ════════════════════════════════════════════════════════════════════════════
# LF-02：Lease 到期后旧 Worker 提交状态 → 拒绝
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lf02_expired_lease_rejects_status_commit():
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
        v1 = await RunRepository(s).claim(run_id, "worker-a", 30)
        await s.commit()

    await _expire_lease(run_id)

    async with AsyncSessionLocal() as s:
        with pytest.raises(RunLeaseLost):
            await RunRepository(s).update_status(
                run_id, status=STATUS_COMPLETED,
                lease=RunExecutionLease(run_id=run_id, worker_id="worker-a", lease_version=v1),
                final_text="stale", finished=True,
            )
        row = await RunRepository(s).get_run(run_id)
    assert row.status == STATUS_RUNNING, "过期 Worker 不得写入终态"
    assert row.final_text is None


# ════════════════════════════════════════════════════════════════════════════
# LF-03：Worker B 接管后 Worker A 提交 → 拒绝
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lf03_takeover_rejects_old_worker_commit():
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
        v1 = await RunRepository(s).claim(run_id, "worker-a", 30)
        await s.commit()

    await _expire_lease(run_id)
    async with AsyncSessionLocal() as s:
        v2 = await RunRepository(s).claim(run_id, "worker-b", 30)  # Worker B 接管
        await s.commit()
    assert v2 is not None and v2 != v1

    lease_a = RunExecutionLease(run_id=run_id, worker_id="worker-a", lease_version=v1)
    lease_b = RunExecutionLease(run_id=run_id, worker_id="worker-b", lease_version=v2)

    async with AsyncSessionLocal() as s:
        # Worker A 的一切写入被拒：状态提交 + 心跳
        with pytest.raises(RunLeaseLost):
            await RunRepository(s).update_status(
                run_id, status=STATUS_FAILED, lease=lease_a, error="stale",
            )
        assert await RunRepository(s).heartbeat(run_id, "worker-a", v1, 30) == HEARTBEAT_LOST

        row = await RunRepository(s).get_run(run_id)
        assert row.worker_id == "worker-b" and row.lease_version == v2
        assert row.status == STATUS_RUNNING and row.error is None

        # Worker B 的提交正常通过
        assert await RunRepository(s).update_status(
            run_id, status=STATUS_COMPLETED, lease=lease_b,
            final_text="ok", finished=True,
        )
        await s.commit()
        row = await RunRepository(s).get_run(run_id)
    assert row.status == STATUS_COMPLETED and row.final_text == "ok"


# ════════════════════════════════════════════════════════════════════════════
# LF-04：Fencing 失败后执行完成路径 → 不写 Session、不发布完成事件
# ════════════════════════════════════════════════════════════════════════════


def _coro(value):
    async def _inner(*a, **k):
        return value
    return _inner()


@pytest.mark.asyncio
async def test_lf04_fencing_failure_blocks_writeback_and_completion_event(monkeypatch):
    """真实 _drive 完成路径 + 租约易主：提交被拒后不写 Session、不发布完成事件。"""
    monkeypatch.setattr(
        AgentRuntimeService, "_load_model",
        lambda self, mid: _coro(({"provider": "mock"}, {"name": "mock"})),
    )
    monkeypatch.setattr(
        "app.services.agent_runtime.service.build_agent",
        lambda *a, **k: object(),
    )

    async def fake_consume(self, agent, payload, run_config, ctx, run, bus):
        run.final_text_parts.append("done")

    monkeypatch.setattr(AgentRuntimeService, "_consume_stream", fake_consume)

    write_backs = []

    async def fake_write_back(self, run):
        write_backs.append(run.run_id)

    monkeypatch.setattr(AgentRuntimeService, "_write_back_session", fake_write_back)

    svc = _make_service(worker_id="w-lf04")
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
    row = await svc.worker.claim_run(run_id)
    assert row is not None
    handle = svc.registry.attach(
        run_id=run_id, agent_id=1, session_id=None, user_id=1,
        thread_id=row.thread_id, status=STATUS_RUNNING,
    )
    handle.lease_version = row.lease_version
    handle.worker_id = "w-lf04"
    handle.lease_expires_at = row.lease_expires_at

    # 租约易主（Worker B 接管）后，旧 Worker 的完成路径必须整体失败
    await _steal_lease(run_id, "w-intruder")

    with pytest.raises(RunLeaseLost):
        await svc._drive(
            handle, LegacyEventAdapter(run_id),
            messages=[{"role": "user", "content": "x"}], resume=None,
            agent_config={"id": 1, "tools": []}, user_id=1,
        )

    assert write_backs == [], "Fencing 失败后不得写 Session"
    async with AsyncSessionLocal() as s:
        got = await RunRepository(s).get_run(run_id)
        events = await EventRepository(s).list_after(run_id, 0)
    assert got.status == STATUS_RUNNING, "不得提交 completed"
    types = [e.event_type for e in events]
    assert "run_completed" not in types and "run_failed" not in types, \
        "不得发布任何终态事件"
    assert "run_suspended" in types, "本地流应以 run_suspended(lease_lost) 边界终止"


# ════════════════════════════════════════════════════════════════════════════
# LF-05：completed 后触发失败路径 → 不允许降级为 failed
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lf05_completed_not_downgraded_to_failed(monkeypatch):
    svc = _make_service(worker_id="w-lf05")
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
    row = await svc.worker.claim_run(run_id)
    assert row is not None
    handle = svc.registry.attach(
        run_id=run_id, agent_id=1, session_id=None, user_id=1,
        thread_id=row.thread_id, status=STATUS_RUNNING,
    )
    handle.lease_version = row.lease_version
    handle.worker_id = "w-lf05"
    handle.lease_expires_at = row.lease_expires_at
    lease = handle.execution_lease()

    # 正常提交 completed
    handle.transition(STATUS_COMPLETED)
    await svc._commit(handle, status=STATUS_COMPLETED, final_text="ok", finished=True)

    # 1) 仓库层：终态不可覆盖（同一有效租约也不行）
    async with AsyncSessionLocal() as s:
        with pytest.raises(IllegalTransition):
            await RunRepository(s).update_status(
                run_id, status=STATUS_FAILED, lease=lease, error="late failure",
            )
        with pytest.raises(IllegalTransition):
            await RunRepository(s).update_status(
                run_id, status=STATUS_CANCELLED, lease=lease,
            )

    # 2) 服务层失败路径：不写库、不发布 RUN_FAILED、不写回 Session
    write_backs = []

    async def fake_write_back(self, run):
        write_backs.append(run.run_id)

    monkeypatch.setattr(AgentRuntimeService, "_write_back_session", fake_write_back)
    await svc._fail(handle, "boom", status=STATUS_FAILED, bus=handle.bus)

    assert write_backs == []
    async with AsyncSessionLocal() as s:
        got = await RunRepository(s).get_run(run_id)
        events = await EventRepository(s).list_after(run_id, 0)
    assert got.status == STATUS_COMPLETED, "completed 不得被失败路径覆盖"
    assert got.error is None
    assert "run_failed" not in [e.event_type for e in events]


# ════════════════════════════════════════════════════════════════════════════
# LF-06：pending Cancel 与 Claim 并发 → 只有一个成功
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lf06_pending_cancel_vs_claim_single_winner():
    # 1) 取消先行：Claim 必须失败
    async with AsyncSessionLocal() as s:
        run1, _ = await _create_run(RunRepository(s))
        await s.commit()
        run1_id = run1.id
        assert await RunRepository(s).cancel_without_lease(run1_id) is True
        await s.commit()
        assert await RunRepository(s).claim(run1_id, "worker-a", 30) is None
        row = await RunRepository(s).get_run(run1_id)
        assert row.status == STATUS_CANCELLED

    # 2) Claim 先行：无 Lease 取消必须失败（不得覆盖正在执行的 Run）
    async with AsyncSessionLocal() as s:
        run2, _ = await _create_run(RunRepository(s))
        await s.commit()
        run2_id = run2.id
        assert await RunRepository(s).claim(run2_id, "worker-a", 30) is not None
        await s.commit()
        assert await RunRepository(s).cancel_without_lease(run2_id) is False
        row = await RunRepository(s).get_run(run2_id)
        assert row.status == STATUS_RUNNING

    # 3) 无 Lease 取消只作用于 pending：已完成/暂停态不会被它覆盖
    async with AsyncSessionLocal() as s:
        run3, _ = await _create_run(RunRepository(s))
        await s.commit()
        v = await RunRepository(s).claim(run3.id, "worker-a", 30)
        await RunRepository(s).update_status(
            run3.id, status=STATUS_WAITING_APPROVAL,
            lease=RunExecutionLease(run_id=run3.id, worker_id="worker-a", lease_version=v),
        )
        await s.commit()
        assert await RunRepository(s).cancel_without_lease(run3.id) is False  # 默认仅 pending


# ════════════════════════════════════════════════════════════════════════════
# LF-07：Tool Ledger 旧 Worker 写入 → 拒绝
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lf07_tool_ledger_rejects_stale_worker():
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
        v1 = await RunRepository(s).claim(run_id, "worker-a", 30)
        await s.commit()
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="tc-lf07", tool_name="calculator",
            args_snapshot={"expression": "1+1"}, args_hash="h", idempotency_key="k",
        )
        await s.commit()
        exec_id = rec.id

    v2 = await _steal_lease(run_id, "worker-b")
    lease_a = RunExecutionLease(run_id=run_id, worker_id="worker-a", lease_version=v1)
    lease_b = RunExecutionLease(run_id=run_id, worker_id="worker-b", lease_version=v2)

    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        # 旧 Worker 的一切 Ledger 写入被拒
        with pytest.raises(RunLeaseLost):
            await ledger.mark_executing(exec_id, lease=lease_a)
        with pytest.raises(RunLeaseLost):
            await ledger.mark_succeeded(exec_id, result_payload="1", lease=lease_a)
        with pytest.raises(RunLeaseLost):
            await ledger.mark_failed(exec_id, error="x", lease=lease_a)

        rec = await ledger.get(run_id, "tc-lf07")
        assert rec.status == TOOL_EXEC_PREPARED  # 状态未被旧 Worker 篡改

        # 有效执行权可以写入
        assert await ledger.mark_executing(exec_id, lease=lease_b) is True
        assert await ledger.mark_succeeded(exec_id, result_payload="2", lease=lease_b) is True
        await s.commit()
        rec = await ledger.get(run_id, "tc-lf07")
    assert rec.status == "succeeded" and rec.result_payload == "2"


# ════════════════════════════════════════════════════════════════════════════
# 数据库级状态机闭环：非法迁移 / 终态覆盖 / expected_status 全部显式拒绝
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_db_state_machine_rejects_illegal_transitions():
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id
        v1 = await RunRepository(s).claim(run_id, "w-a", 30)
        await s.commit()
        lease1 = RunExecutionLease(run_id=run_id, worker_id="w-a", lease_version=v1)

        # running 不是合法目标（状态机闭环）
        with pytest.raises(IllegalTransition):
            await RunRepository(s).update_status(run_id, status=STATUS_RUNNING, lease=lease1)
        # pending 不是合法目标（终态回退）
        with pytest.raises(IllegalTransition):
            await RunRepository(s).update_status(run_id, status=STATUS_PENDING, lease=lease1)
        # expected_status 不匹配 → 显式拒绝
        with pytest.raises(IllegalTransition):
            await RunRepository(s).update_status(
                run_id, status=STATUS_COMPLETED, lease=lease1,
                expected_status=STATUS_PENDING,
            )
        # 合法：running → waiting_approval
        assert await RunRepository(s).update_status(
            run_id, status=STATUS_WAITING_APPROVAL, lease=lease1,
        )
        await s.commit()

        # waiting_approval → completed 不合法（必须回到 running 再完成）
        with pytest.raises(IllegalTransition):
            await RunRepository(s).update_status(
                run_id, status=STATUS_COMPLETED, lease=lease1,
            )
        # 人工恢复路径回到 running
        v2 = await RunRepository(s).claim_paused(run_id, "w-a", 30)
        await s.commit()
        lease2 = RunExecutionLease(run_id=run_id, worker_id="w-a", lease_version=v2)
        assert await RunRepository(s).update_status(
            run_id, status=STATUS_COMPLETED, lease=lease2, finished=True,
        )
        await s.commit()

        # 终态单向不可覆盖：completed → 其他任何状态都被拒
        for target in (
            STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED,
            STATUS_PENDING, STATUS_RUNNING, STATUS_WAITING_APPROVAL,
            STATUS_NEEDS_RECONCILIATION,
        ):
            with pytest.raises(IllegalTransition):
                await RunRepository(s).update_status(run_id, status=target, lease=lease2)

        row = await RunRepository(s).get_run(run_id)
        assert row.status == STATUS_COMPLETED and row.finished_at is not None


# ════════════════════════════════════════════════════════════════════════════
# 多 Worker 真实独立进程：并发 Claim / 跨进程 Fencing
# ════════════════════════════════════════════════════════════════════════════

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD_CLAIM = r"""
import asyncio, sys
from app.core.database import AsyncSessionLocal
from app.services.agent_runtime.repositories import RunRepository

async def main():
    run_id, worker_id = sys.argv[1], sys.argv[2]
    async with AsyncSessionLocal() as s:
        v = await RunRepository(s).claim(run_id, worker_id, 60)
        await s.commit()
    print(("OK:%s" % v) if v is not None else "NONE")

asyncio.run(main())
"""

_CHILD_FENCE = r"""
import asyncio, os, sys, time
from app.core.database import AsyncSessionLocal
from app.services.agent_runtime.lifecycle import RunExecutionLease
from app.services.agent_runtime.repositories import RunRepository
from app.services.agent_runtime.repositories.errors import RunLeaseLost

async def main():
    run_id, worker_id, go_file = sys.argv[1], sys.argv[2], sys.argv[3]
    async with AsyncSessionLocal() as s:
        v = await RunRepository(s).claim(run_id, worker_id, 60)
        await s.commit()
    if v is None:
        print("CLAIM:NONE", flush=True)
        return
    print("CLAIM:%s" % v, flush=True)
    deadline = time.time() + 30
    while not os.path.exists(go_file):
        if time.time() > deadline:
            print("TIMEOUT", flush=True)
            return
        time.sleep(0.02)
    async with AsyncSessionLocal() as s:
        try:
            await RunRepository(s).update_status(
                run_id, status="completed",
                lease=RunExecutionLease(run_id=run_id, worker_id=worker_id, lease_version=v),
                finished=True,
            )
            await s.commit()
            print("COMMIT:OK", flush=True)
        except RunLeaseLost:
            print("COMMIT:REJECTED:RunLeaseLost", flush=True)
        except Exception as e:
            print("COMMIT:REJECTED:%s" % type(e).__name__, flush=True)

asyncio.run(main())
"""


def _child_env() -> dict:
    env = os.environ.copy()
    env.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
    return env


@pytest.mark.asyncio
async def test_multiprocess_concurrent_claim_single_winner():
    """两个真实 OS 进程并发 Claim 同一 pending Run：只有一个成功。"""
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD_CLAIM, run_id, f"proc-{i}"],
            cwd=_BACKEND_DIR, env=_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(2)
    ]
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"子进程失败:\n{out}\n{err}"
        outputs.append(out.strip())

    winners = [o for o in outputs if o.startswith("OK:")]
    assert len(winners) == 1, f"必须恰好一个 Claim 成功，实际: {outputs}"
    assert outputs.count("NONE") == 1

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
        assert row.status == STATUS_RUNNING
        assert row.worker_id in ("proc-0", "proc-1")


@pytest.mark.asyncio
async def test_multiprocess_fencing_rejects_stale_commit(tmp_path):
    """真实独立进程：Worker A 领取后被接管，A 的状态提交必须被拒绝。"""
    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id

    go_file = str(tmp_path / "go.flag")
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_FENCE, run_id, "proc-a", go_file],
        cwd=_BACKEND_DIR, env=_child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    claim_line = proc.stdout.readline().strip()
    assert claim_line.startswith("CLAIM:"), claim_line
    a_version = int(claim_line.split(":", 1)[1])

    # 父进程（另一 OS 进程）作为 Worker B：接管租约
    await _expire_lease(run_id)
    async with AsyncSessionLocal() as s:
        b_version = await RunRepository(s).claim(run_id, "proc-b", 60)
        await s.commit()
    assert b_version is not None and b_version > a_version

    open(go_file, "w").close()  # 放行子进程提交
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, f"子进程失败:\n{out}\n{err}"

    lines = out.strip().splitlines()
    assert "COMMIT:REJECTED:RunLeaseLost" in lines, \
        f"旧 Worker 提交必须被拒绝，实际输出: {out}"
    assert "COMMIT:OK" not in lines

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
        assert row.status == STATUS_RUNNING
        assert row.worker_id == "proc-b" and row.lease_version == b_version


# ════════════════════════════════════════════════════════════════════════════
# 优雅停机 graceful drain：在途 Run 正常收尾（不是超时挂起）
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_stop_graceful_drain_completes_inflight(monkeypatch):
    record = []

    async def slow_drive(
        self, run, adapter, *, messages, resume,
        agent_config=None, use_memory_context="", db_info=None, user_id=None,
    ):
        record.append(run.run_id)
        await asyncio.sleep(0.3)
        run.final_text = "ok"
        run.transition(STATUS_COMPLETED)
        await self._commit(run, status=STATUS_COMPLETED, final_text="ok", finished=True)

    monkeypatch.setattr(AgentRuntimeService, "_drive", slow_drive)
    svc = _make_service(worker_id="w-drain", shutdown_grace=10.0)

    async with AsyncSessionLocal() as s:
        run, _ = await _create_run(RunRepository(s))
        await s.commit()
        run_id = run.id

    row = await svc.worker.claim_run(run_id)
    assert row is not None
    task = asyncio.create_task(svc.worker.run_claimed(row, RunJob(run_id=run_id)))
    while not record:
        await asyncio.sleep(0.01)

    await svc.worker.stop()  # graceful：等待在途执行自然完成
    await asyncio.gather(task, return_exceptions=True)

    async with AsyncSessionLocal() as s:
        got = await RunRepository(s).get_run(run_id)
    assert got.status == STATUS_COMPLETED, "优雅停机应让在途 Run 正常收尾"
    assert got.worker_id is None  # 租约已释放

"""P0-03 Tool Ledger 执行权与调用身份一致性（任务书 §5.3 验收，Batch 3）。

逐条对应 §5.3 七项验收：
  1. Worker A 不能修改 Worker B 接管后的 Ledger；
  2. 相同 tool_call_id + 不同 args_hash 必须冲突；
  3. 相同 tool_call_id + 不同 tool_name 必须冲突；
  4. Ledger 更新失败时不发布 TOOL_COMPLETED；
  5. 成功记录不能被失败覆盖；
  6. 未知副作用记录不能被普通 Agent 自动覆盖；
  7. 并发执行同一个 tool_call_id 时，最多只有一个有效执行者。

运行：
    pytest backend/tests/test_tool_ledger_guard.py -q
"""
import asyncio
import socket
import uuid

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
from app.services.agent_runtime.context import RunScope, RuntimeContext
from app.services.agent_runtime.events import (
    TOOL_COMPLETED,
    TOOL_FAILED,
    EventBus,
    RuntimeEvent,
)
from app.services.agent_runtime.lifecycle import RunExecutionLease
from app.services.agent_runtime.repositories import (
    RunRepository,
    ToolExecutionRepository,
)
from app.services.agent_runtime.repositories.errors import (
    IllegalTransition,
    RunLeaseLost,
    ToolCallIdentityConflict,
)
from app.services.agent_runtime.repositories.tool_execution_repository import (
    TOOL_EXEC_EXECUTING,
    TOOL_EXEC_NEEDS_RECONCILIATION,
    TOOL_EXEC_PREPARED,
    TOOL_EXEC_SUCCEEDED,
    TOOL_EXEC_UNKNOWN,
)
from app.services.agent_runtime.tools.adapter import ToolAdapterFactory, ToolLedgerError
from app.services.agent_runtime.tools.policy import ToolPolicy
from app.services.tools import ToolExecutor
from app.services.tools.result import ToolResult


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
    # 真实 PG/MySQL 有外键约束：先确保 agent 行存在
    agent = await repo.session.get(Agent, 1)
    if agent is None:
        agent = Agent(id=1, name="guard-agent")
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


async def _claimed_run(worker_id: str = "w-tool"):
    """创建真实 AgentRun 并 claim 执行权 -> (run_id, lease_version)。"""
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, worker_id, 30)
        await s.commit()
    return run.id, version


def _lease_for(run_id: str, version: int, worker_id: str = "w-tool") -> RunExecutionLease:
    return RunExecutionLease(run_id=run_id, worker_id=worker_id, lease_version=version)


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


def _make_ctx(run_id: str, worker_id: str = "w-tool", lease_version: int = 1) -> RuntimeContext:
    return RuntimeContext(
        scope=RunScope(user_id=1),
        run_id=run_id,
        thread_id=f"thr-{run_id}",
        agent_id=1,
        worker_id=worker_id,
        lease_version=lease_version,
    )


class _RecordingBus(EventBus):
    """记录发布事件类型的 EventBus（验收第 4 条：断言无 TOOL_COMPLETED）。"""

    def __init__(self):
        super().__init__()
        self.published: list = []

    async def publish(self, ev: RuntimeEvent) -> None:
        self.published.append(ev.type)
        await super().publish(ev)


def _make_adapter(ctx: RuntimeContext, tool_names, bus=None) -> ToolAdapterFactory:
    policy = ToolPolicy.from_settings(set(tool_names), enable_hitl=False)
    return ToolAdapterFactory(ctx, policy, bus or EventBus())


def _fake_execute(behavior):
    """ToolExecutor.execute 替身（真实账本 + 可注入执行体）。"""

    async def _exec(self, name, args, *, raise_on_error=False):
        return await behavior(name, args)

    return _exec


async def _begin_entry(run_id, lease, *, tool_call_id, tool_name="webhook_notify",
                       args_hash="h1", idempotency_key="k1"):
    async with AsyncSessionLocal() as s:
        rec, created = await ToolExecutionRepository(s).begin(
            run_id=run_id, tool_call_id=tool_call_id, tool_name=tool_name,
            args_hash=args_hash, args_snapshot={"x": 1}, idempotency_key=idempotency_key,
            lease=lease,
        )
        await s.commit()
    assert created
    return rec.id


# ════════════════════════════════════════════════════════════════════════════
# 验收 1：Worker A 不能修改 Worker B 接管后的 Ledger
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ledger_rejects_stale_worker_after_takeover():
    run_id, v1 = await _claimed_run("worker-a")
    lease_a = _lease_for(run_id, v1, "worker-a")
    exec_id = await _begin_entry(run_id, lease_a, tool_call_id="tc-takeover")

    v2 = await _steal_lease(run_id, "worker-b")
    lease_b = _lease_for(run_id, v2, "worker-b")

    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        # 旧 Worker 的一切 Ledger 写入被拒（含重放登记 begin）
        with pytest.raises(RunLeaseLost):
            await repo.begin(
                run_id=run_id, tool_call_id="tc-takeover", tool_name="webhook_notify",
                args_hash="h1", args_snapshot={"x": 1}, idempotency_key="k1",
                lease=lease_a,
            )
        with pytest.raises(RunLeaseLost):
            await repo.mark_executing(exec_id, lease=lease_a)
        with pytest.raises(RunLeaseLost):
            await repo.mark_succeeded(exec_id, result_payload="x", lease=lease_a)
        with pytest.raises(RunLeaseLost):
            await repo.mark_failed(exec_id, error="x", lease=lease_a)
        with pytest.raises(RunLeaseLost):
            await repo.mark_unknown(exec_id, error="x", lease=lease_a)
        with pytest.raises(RunLeaseLost):
            await repo.mark_needs_reconciliation(exec_id, error="x", lease=lease_a)

        rec = await repo.get(run_id, "tc-takeover")
        assert rec.status == TOOL_EXEC_PREPARED, "旧 Worker 不得篡改 Ledger 状态"

        # 新执行权写入正常
        assert await repo.mark_executing(exec_id, lease=lease_b) is True
        assert await repo.mark_succeeded(exec_id, result_payload="ok", lease=lease_b) is True
        await s.commit()
        rec = await repo.get(run_id, "tc-takeover")
    assert rec.status == TOOL_EXEC_SUCCEEDED and rec.result_payload == "ok"


# ════════════════════════════════════════════════════════════════════════════
# 验收 2：相同 tool_call_id + 不同 args_hash 必须冲突
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_same_tool_call_id_different_args_hash_conflicts():
    run_id, v1 = await _claimed_run()
    lease = _lease_for(run_id, v1)

    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        await repo.begin(
            run_id=run_id, tool_call_id="tc-args", tool_name="http_post",
            args_hash="hash-a", args_snapshot={"a": 1}, idempotency_key="k", lease=lease,
        )
        await s.commit()
        with pytest.raises(ToolCallIdentityConflict):
            await repo.begin(
                run_id=run_id, tool_call_id="tc-args", tool_name="http_post",
                args_hash="hash-b", args_snapshot={"a": 2}, idempotency_key="k", lease=lease,
            )
        await s.rollback()
        rec = await repo.get(run_id, "tc-args")
        assert rec.args_hash == "hash-a", "冲突登记不得污染原记录"

    # None 一致性：已存 None != 本次 hash 同样冲突
    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        await repo.begin(
            run_id=run_id, tool_call_id="tc-none", tool_name="http_post",
            args_hash=None, args_snapshot=None, idempotency_key=None, lease=lease,
        )
        await s.commit()
        with pytest.raises(ToolCallIdentityConflict):
            await repo.begin(
                run_id=run_id, tool_call_id="tc-none", tool_name="http_post",
                args_hash="hash-x", args_snapshot=None, idempotency_key=None, lease=lease,
            )
        await s.rollback()


# ════════════════════════════════════════════════════════════════════════════
# 验收 3：相同 tool_call_id + 不同 tool_name 必须冲突
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_same_tool_call_id_different_tool_name_conflicts():
    run_id, v1 = await _claimed_run()
    lease = _lease_for(run_id, v1)

    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        await repo.begin(
            run_id=run_id, tool_call_id="tc-name", tool_name="http_post",
            args_hash="h", args_snapshot={}, idempotency_key="k", lease=lease,
        )
        await s.commit()
        with pytest.raises(ToolCallIdentityConflict):
            await repo.begin(
                run_id=run_id, tool_call_id="tc-name", tool_name="send_email_tool",
                args_hash="h", args_snapshot={}, idempotency_key="k", lease=lease,
            )
        await s.rollback()
        rec = await repo.get(run_id, "tc-name")
        assert rec.tool_name == "http_post"

    # idempotency_key 不一致同样冲突（含 None 一致性）
    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        await repo.begin(
            run_id=run_id, tool_call_id="tc-key", tool_name="http_post",
            args_hash="h", args_snapshot={}, idempotency_key=None, lease=lease,
        )
        await s.commit()
        with pytest.raises(ToolCallIdentityConflict):
            await repo.begin(
                run_id=run_id, tool_call_id="tc-key", tool_name="http_post",
                args_hash="h", args_snapshot={}, idempotency_key="k2", lease=lease,
            )
        await s.rollback()


# ════════════════════════════════════════════════════════════════════════════
# 验收 4：Ledger 更新失败时不发布 TOOL_COMPLETED
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ledger_write_failure_publishes_no_tool_completed(monkeypatch):
    """执行成功但终态确认时执行权已易主：RunLeaseLost 冒泡，不得发布 TOOL_COMPLETED。"""
    run_id, v1 = await _claimed_run("w-a")
    bus = _RecordingBus()
    adapter = _make_adapter(_make_ctx(run_id, "w-a", v1), ["calculator"], bus)

    async def behavior(name, args):
        # 执行期间租约被夺：终态确认必然 RunLeaseLost
        await _steal_lease(run_id, "w-intruder")
        return ToolResult.succeeded("4")

    monkeypatch.setattr(ToolExecutor, "execute", _fake_execute(behavior))

    with pytest.raises(RunLeaseLost):
        await adapter._execute("calculator", {"expression": "2+2", "tool_call_id": "tc-04"})

    assert TOOL_COMPLETED not in bus.published, "Ledger 更新失败不得发布 TOOL_COMPLETED"
    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-04")
    assert rec.status == TOOL_EXEC_EXECUTING, "终态确认被拒后不得伪装为 succeeded"


@pytest.mark.asyncio
async def test_ledger_persistence_error_publishes_no_tool_completed(monkeypatch):
    """Ledger 一般持久化失败：ToolLedgerError 中止 Graph，不发布 TOOL_COMPLETED。"""
    run_id, v1 = await _claimed_run()
    bus = _RecordingBus()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=v1), ["calculator"], bus)

    async def behavior(name, args):
        return ToolResult.succeeded("4")

    monkeypatch.setattr(ToolExecutor, "execute", _fake_execute(behavior))

    async def _boom(self, exec_id, **kw):
        raise RuntimeError("ledger db down")

    monkeypatch.setattr(ToolExecutionRepository, "mark_succeeded", _boom)

    with pytest.raises(ToolLedgerError):
        await adapter._execute("calculator", {"expression": "2+2", "tool_call_id": "tc-db"})

    assert TOOL_COMPLETED not in bus.published


# ════════════════════════════════════════════════════════════════════════════
# 验收 5：成功记录不能被失败覆盖
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_succeeded_record_cannot_be_overwritten():
    run_id, v1 = await _claimed_run()
    lease = _lease_for(run_id, v1)
    exec_id = await _begin_entry(run_id, lease, tool_call_id="tc-immutable")

    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        assert await repo.mark_executing(exec_id, lease=lease) is True
        assert await repo.mark_succeeded(exec_id, result_payload="good", lease=lease) is True
        await s.commit()

    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        with pytest.raises(IllegalTransition):
            await repo.mark_failed(exec_id, error="boom", lease=lease)
        with pytest.raises(IllegalTransition):
            await repo.mark_unknown(exec_id, error="?", lease=lease)
        with pytest.raises(IllegalTransition):
            await repo.mark_succeeded(exec_id, result_payload="new", lease=lease)
        with pytest.raises(IllegalTransition):
            await repo.mark_needs_reconciliation(exec_id, error="?", lease=lease)
        await s.rollback()
        rec = await repo.get(run_id, "tc-immutable")
    assert rec.status == TOOL_EXEC_SUCCEEDED, "成功记录不得被失败/未知覆盖"
    assert rec.result_payload == "good", "成功结果不得被改写"


# ════════════════════════════════════════════════════════════════════════════
# 验收 6：未知副作用记录不能被普通 Agent 自动覆盖
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unknown_record_not_auto_overwritten_by_agent(monkeypatch):
    """non_idempotent 工具的 unknown 记录：禁止自动重放，绝不覆盖为 succeeded。"""
    run_id, v1 = await _claimed_run()
    tc = "tc-unknown"
    adapter_args = {
        "webhook_url": "https://example.com/hook", "message": "m", "tool_call_id": tc,
    }
    executed = []

    async def behavior(name, args):
        executed.append(name)
        return ToolResult.unknown("timeout：无法确认通知是否已发出")

    monkeypatch.setattr(ToolExecutor, "execute", _fake_execute(behavior))
    bus = _RecordingBus()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=v1), ["webhook_notify"], bus)

    # 第一次执行：结果未知 -> 账本 unknown（由 adapter 建账，身份字段天然一致）
    await adapter._execute("webhook_notify", dict(adapter_args))
    assert executed == ["webhook_notify"]

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, tc)
        exec_id = rec.id
    assert rec.status == TOOL_EXEC_UNKNOWN

    # 仓库层：unknown 不可被普通路径覆盖（仅可转 needs_reconciliation 人工核对）
    lease = _lease_for(run_id, v1)
    async with AsyncSessionLocal() as s:
        repo = ToolExecutionRepository(s)
        with pytest.raises(IllegalTransition):
            await repo.mark_succeeded(exec_id, result_payload="x", lease=lease)
        with pytest.raises(IllegalTransition):
            await repo.mark_failed(exec_id, error="x", lease=lease)
        await s.rollback()

    # 普通 Agent 重放：禁止执行、禁止覆盖
    text = await adapter._execute("webhook_notify", dict(adapter_args))
    assert executed == ["webhook_notify"], "unknown 副作用禁止自动重放"
    assert "阻止自动重放" in text

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, tc)
    assert rec.status == TOOL_EXEC_NEEDS_RECONCILIATION, "转入人工核对，绝不覆盖为 succeeded/failed"


@pytest.mark.asyncio
async def test_unknown_readonly_retry_allowed_by_outcome_classification(monkeypatch):
    """边界：read_only 工具无副作用，unknown 后经副作用等级判定允许重试（reentry）。"""
    # SSRF 检查做真实 DNS 解析：stub 公网 IP（测试环境可能无外网）
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 80))],
    )
    run_id, v1 = await _claimed_run()
    tc = "tc-readonly"
    executed = []
    outcomes = [
        ToolResult.unknown("connection reset"),
        ToolResult.succeeded("HTTP 200"),
    ]

    async def behavior(name, args):
        executed.append(name)
        return outcomes.pop(0)

    monkeypatch.setattr(ToolExecutor, "execute", _fake_execute(behavior))
    adapter = _make_adapter(_make_ctx(run_id, lease_version=v1), ["http_get"])
    adapter_args = {"url": "https://example.com/a", "tool_call_id": tc}

    await adapter._execute("http_get", dict(adapter_args))
    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, tc)
    assert rec.status == TOOL_EXEC_UNKNOWN

    text = await adapter._execute("http_get", dict(adapter_args))
    assert executed == ["http_get", "http_get"], "read_only 经副作用等级判定允许重试"
    assert text == "HTTP 200"

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, tc)
    assert rec.status == TOOL_EXEC_SUCCEEDED


# ════════════════════════════════════════════════════════════════════════════
# 验收 7：并发执行同一个 tool_call_id 时，最多只有一个有效执行者
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_concurrent_mark_executing_has_single_winner():
    """prepared -> executing 一次性原子翻转：并发竞争恰一个有效执行者。"""
    run_id, v1 = await _claimed_run()
    lease = _lease_for(run_id, v1)
    exec_id = await _begin_entry(run_id, lease, tool_call_id="tc-race")

    async def _try():
        async with AsyncSessionLocal() as s:
            ok = await ToolExecutionRepository(s).mark_executing(exec_id, lease=lease)
            await s.commit()
            return ok

    results = await asyncio.gather(_try(), _try())
    assert sorted(results) == [False, True], (
        f"并发同一 tool_call_id 必须恰一个有效执行者，实际 {results}"
    )


@pytest.mark.asyncio
async def test_concurrent_execute_same_tool_call_id_runs_once(monkeypatch):
    """adapter 层：并发重放同一 tool_call_id，副作用执行体恰执行一次。"""
    run_id, v1 = await _claimed_run()
    tc = "tc-concurrent"
    calls = []

    async def behavior(name, args):
        calls.append(name)
        await asyncio.sleep(0.05)  # 拉开交错窗口
        return ToolResult.succeeded("42")

    monkeypatch.setattr(ToolExecutor, "execute", _fake_execute(behavior))

    # 双方到达 mark_executing 后再竞争原子翻转（最大化交错确定性）
    orig_mark_executing = ToolExecutionRepository.mark_executing
    arrived = 0
    both_arrived = asyncio.Event()

    async def _synced_mark_executing(self, exec_id, *, lease, reentry=False):
        nonlocal arrived
        arrived += 1
        if arrived >= 2:
            both_arrived.set()
        try:
            await asyncio.wait_for(both_arrived.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass  # 兜底防挂：单侧到达也继续（断言仍会验证恰一次执行）
        return await orig_mark_executing(self, exec_id, lease=lease, reentry=reentry)

    monkeypatch.setattr(ToolExecutionRepository, "mark_executing", _synced_mark_executing)

    adapter1 = _make_adapter(_make_ctx(run_id, lease_version=v1), ["calculator"])
    adapter2 = _make_adapter(_make_ctx(run_id, lease_version=v1), ["calculator"])
    r1, r2 = await asyncio.gather(
        adapter1._execute("calculator", {"expression": "6*7", "tool_call_id": tc}),
        adapter2._execute("calculator", {"expression": "6*7", "tool_call_id": tc}),
    )

    assert len(calls) == 1, f"副作用执行体必须恰执行一次，实际 {len(calls)} 次"
    assert "42" in (r1, r2), "有效执行者必须拿到执行结果"
    # 落败方只能拿到执行结果（缓存回放）或显式拒绝，不得重复执行
    for text in (r1, r2):
        assert text == "42" or "阻止自动重放" in text

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, tc)
    assert rec.status == TOOL_EXEC_SUCCEEDED

"""P0-04 验收测试（任务书 §6.2 恢复决策表 / §6.3 验收 / §16.4 RC-01~RC-06）。

覆盖：
- RC-01 Checkpoint 缺失、无副作用记录 -> 按策略安全重启（fresh + recovery mode 记录）
- RC-02 Checkpoint 缺失、read_only 未决 -> 允许安全重试
- RC-03 Checkpoint 缺失、external_idempotent unknown -> 阻止重新生成新调用 ID
- RC-04 Checkpoint 缺失、non_idempotent unknown -> needs_reconciliation
- RC-05 Checkpoint 文件/数据库损坏 -> 安全阻断（真实损坏，不 mock）
- RC-06 Checkpoint 存在、Ledger succeeded -> 返回缓存结果，不重复执行
 （重放复用原 tool_call_id 与幂等键）
- 幂等契约化声明（§6.2 B）：未声明的 HTTP POST 按 non_idempotent；
  external_idempotent 必须声明下游幂等能力与 Key Header
- 所有恢复决策写入日志与 agent_runs.recovery_error / recovery_metadata（§6.3）
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import delete

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import (
    Agent,
    AgentApproval,
    AgentRun,
    AgentRunEvent,
    AgentToolExecution,
)
from app.models.user import User
from app.services.agent_runtime.context import RunScope, RuntimeContext
from app.services.agent_runtime.events import EventBus
from app.services.agent_runtime.lifecycle import (
    RECOVERY_CONTINUE,
    RECOVERY_FRESH,
    RECOVERY_NEEDS_RECONCILIATION,
    STATUS_COMPLETED,
    STATUS_NEEDS_RECONCILIATION,
    RunExecutionLease,
)
from app.services.agent_runtime.persistence import CheckpointerFactory
from app.services.agent_runtime.repositories import RunRepository, ToolExecutionRepository
from app.services.agent_runtime.repositories.tool_execution_repository import (
    TOOL_EXEC_PREPARED,
    TOOL_EXEC_SUCCEEDED,
    TOOL_EXEC_UNKNOWN,
)
from app.services.agent_runtime.service import AgentRuntimeService
from app.services.agent_runtime.tools import classification
from app.services.agent_runtime.tools.adapter import ToolAdapterFactory
from app.services.agent_runtime.tools.classification import (
    CHECKPOINT_MISSING,
    CHECKPOINT_OK,
    CHECKPOINT_UNREADABLE,
    CLASS_EXTERNAL_IDEMPOTENT,
    CLASS_NON_IDEMPOTENT,
    CLASS_READ_ONLY,
    REPLAY_BLOCK,
    RETRY_IDEMPOTENT_KEY,
    SideEffectContract,
    get_side_effect_contract,
    idempotency_key_header,
    recovery_decision,
    register_side_effect_contract,
    replay_action,
    tool_outcome_class,
    unregister_side_effect_contract,
)
from app.services.agent_runtime.tools.policy import ToolPolicy
from app.services.agent_runtime.worker import RunWorker


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        # 真实库（PG/MySQL）外键强约束：按依赖顺序清理
        for model in (AgentApproval, AgentRunEvent, AgentToolExecution, AgentRun):
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


@pytest.fixture
def contracts():
    """契约注册表快照/恢复（隔离用例间的全局声明）。"""
    saved = dict(classification._CONTRACTS)
    yield classification
    classification._CONTRACTS.clear()
    classification._CONTRACTS.update(saved)


def _uid() -> str:
    return uuid.uuid4().hex


async def _create_run(repo: RunRepository, **kw):
    run_id = _uid()
    defaults = dict(
        run_id=run_id,
        agent_id=1,
        session_id=None,
        user_id=1,
        thread_id=f"agent-1-run-{run_id[:8]}",
        input_payload={
            "messages": [{"role": "user", "content": "hi"}],
            "use_memory_context": "",
            "db_info": None,
        },
        agent_config_snapshot={"id": 1, "tools": []},
    )
    defaults.update(kw)
    if await repo.session.get(Agent, 1) is None:
        repo.session.add(Agent(id=1, name="recovery-agent"))
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


async def _claimed_run(worker_id: str = "w-rec"):
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, worker_id, 30)
        await s.commit()
    return run.id, version


def _lease_for(run_id: str, version: int, worker_id: str = "w-rec") -> RunExecutionLease:
    return RunExecutionLease(run_id=run_id, worker_id=worker_id, lease_version=version)


def _entry(tool_name: str, status: str, tool_call_id: str = "tc-1"):
    return SimpleNamespace(tool_name=tool_name, status=status, tool_call_id=tool_call_id)


def _make_service(worker_id: str = "w-recover"):
    svc = AgentRuntimeService()
    svc.worker = RunWorker(
        svc,
        worker_id=worker_id,
        lease_seconds=30,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        shutdown_grace=0.5,
    )
    return svc


def _stub_drive(record):
    from app.services.agent_runtime.events import RUN_COMPLETED, RUN_STARTED, RuntimeEvent
    from app.services.agent_runtime.lifecycle import STATUS_COMPLETED as _SC

    async def fake_drive(
        self, run, adapter, *, messages, resume,
        agent_config=None, use_memory_context="", db_info=None, user_id=None,
    ):
        record.append({"run_id": run.run_id, "messages": messages})
        await run.bus.publish(RuntimeEvent(type=RUN_STARTED, run_id=run.run_id))
        run.final_text = "ok"
        run.summary = {"tool_calls": 0, "model_calls": 1}
        run.transition(_SC)
        await self._commit(
            run, status=_SC, final_text="ok", summary=run.summary, finished=True,
        )
        await run.bus.publish(
            RuntimeEvent(type=RUN_COMPLETED, run_id=run.run_id, data={"final_text": "ok"})
        )

    return fake_drive


async def _wait_status(run_id, statuses, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with AsyncSessionLocal() as s:
            row = await RunRepository(s).get_run(run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.02)
    return None


def _make_ctx(run_id: str, lease_version: int = 1, worker_id: str = "w-rec") -> RuntimeContext:
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
    class _Fake:
        def __init__(self, db, agent_config):
            pass

        async def execute(self, name, args, *, raise_on_error=False):
            return await behavior(name, args)

    return _Fake


# ════════════════════════════════════════════════════════════════════════════
# §6.2 B 幂等能力必须由工具配置声明
# ════════════════════════════════════════════════════════════════════════════


def test_undeclared_http_post_is_non_idempotent(contracts):
    """未声明的 HTTP POST 一律按 non_idempotent 处理（§6.2 B / TL-09）。"""
    c = get_side_effect_contract("http_post")
    assert c.side_effect_class == CLASS_NON_IDEMPOTENT
    assert c.downstream_idempotency_supported is False
    assert idempotency_key_header("http_post") is None  # 不注入幂等键
    assert replay_action(TOOL_EXEC_UNKNOWN, "http_post") == REPLAY_BLOCK


def test_external_idempotent_requires_downstream_declaration(contracts):
    """标记 external_idempotent 必须声明下游幂等能力 + Key Header，否则拒绝登记。"""
    with pytest.raises(ValueError):
        register_side_effect_contract(SideEffectContract(
            tool_name="order_api",
            side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
        ))
    with pytest.raises(ValueError):
        register_side_effect_contract(SideEffectContract(
            tool_name="order_api",
            side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
            downstream_idempotency_supported=True,  # 缺 idempotency_key_header
        ))
    register_side_effect_contract(SideEffectContract(
        tool_name="order_api",
        side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
        downstream_idempotency_supported=True,
        idempotency_key_header="X-Idempotency-Key",
        retry_policy=RETRY_IDEMPOTENT_KEY,
        external_operation_lookup="GET /orders/{key}",
    ))
    assert tool_outcome_class("order_api") == CLASS_EXTERNAL_IDEMPOTENT
    assert idempotency_key_header("order_api") == "X-Idempotency-Key"
    # 契约字段完整保留（人工核对线索）
    c = get_side_effect_contract("order_api")
    assert c.retry_policy == RETRY_IDEMPOTENT_KEY
    assert c.external_operation_lookup == "GET /orders/{key}"


def test_readonly_tools_have_safe_retry_contract(contracts):
    assert tool_outcome_class("calculator") == CLASS_READ_ONLY
    assert get_side_effect_contract("calculator").retry_policy == "safe_retry"
    assert tool_outcome_class("python_exec") == CLASS_NON_IDEMPOTENT


# ════════════════════════════════════════════════════════════════════════════
# §6.2 A 恢复决策表（decision 级）
# ════════════════════════════════════════════════════════════════════════════


def test_rc01_missing_checkpoint_no_unresolved_starts_fresh():
    d = recovery_decision(CHECKPOINT_MISSING, [])
    assert d.mode == RECOVERY_FRESH
    assert "recovery mode=fresh" in d.reason
    # prepared 只是执行前登记、尚未产生副作用，不阻断 fresh 重启
    d2 = recovery_decision(CHECKPOINT_MISSING, [_entry("webhook_notify", TOOL_EXEC_PREPARED)])
    assert d2.mode == RECOVERY_FRESH


def test_rc02_missing_checkpoint_readonly_unresolved_safe_retry():
    d = recovery_decision(
        CHECKPOINT_MISSING, [_entry("calculator", TOOL_EXEC_UNKNOWN)]
    )
    assert d.mode == RECOVERY_FRESH
    assert "read_only" in d.reason


def test_rc03_missing_checkpoint_idempotent_unknown_blocks_new_call_id(contracts):
    register_side_effect_contract(SideEffectContract(
        tool_name="order_api",
        side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
        downstream_idempotency_supported=True,
        idempotency_key_header="X-Idempotency-Key",
    ))
    d = recovery_decision(
        CHECKPOINT_MISSING, [_entry("order_api", TOOL_EXEC_UNKNOWN)]
    )
    assert d.mode == RECOVERY_NEEDS_RECONCILIATION
    assert "禁止重新推理生成新调用 ID" in d.reason  # RC-03：阻止生成新调用 ID
    assert d.pending[0]["outcome_class"] == CLASS_EXTERNAL_IDEMPOTENT


def test_rc04_missing_checkpoint_non_idempotent_unknown_reconciles():
    d = recovery_decision(
        CHECKPOINT_MISSING, [_entry("webhook_notify", TOOL_EXEC_UNKNOWN)]
    )
    assert d.mode == RECOVERY_NEEDS_RECONCILIATION


def test_ok_checkpoint_decisions(contracts):
    # 全部已确认 -> 从 Checkpoint 继续
    d = recovery_decision(CHECKPOINT_OK, [])
    assert d.mode == RECOVERY_CONTINUE
    # read_only 未决 -> 允许安全重试
    d = recovery_decision(CHECKPOINT_OK, [_entry("calculator", TOOL_EXEC_UNKNOWN)])
    assert d.mode == RECOVERY_CONTINUE
    # non_idempotent 未决 -> needs_reconciliation
    d = recovery_decision(CHECKPOINT_OK, [_entry("webhook_notify", TOOL_EXEC_UNKNOWN)])
    assert d.mode == RECOVERY_NEEDS_RECONCILIATION
    # 契约声明的 external_idempotent 未决 -> 复用原 key 重试（原 tool_call_id
    # 由 Checkpoint 保持，见 RC-06 测试）
    register_side_effect_contract(SideEffectContract(
        tool_name="order_api",
        side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
        downstream_idempotency_supported=True,
        idempotency_key_header="X-Idempotency-Key",
    ))
    d = recovery_decision(CHECKPOINT_OK, [_entry("order_api", TOOL_EXEC_UNKNOWN)])
    assert d.mode == RECOVERY_CONTINUE
    assert "原 tool_call_id" in d.reason


# ════════════════════════════════════════════════════════════════════════════
# RC-01 / RC-02：Checkpoint 缺失的安全恢复（worker 集成）
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rc01_worker_missing_checkpoint_no_side_effect_restarts(monkeypatch):
    """RC-01：无 Checkpoint 且无未决副作用 -> 按输入快照重新开始并记录 recovery mode。"""
    record: list = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-rc01")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await repo.claim(run.id, "dead-worker", 30)
        await s.commit()
        run_id = run.id
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_COMPLETED})
    finally:
        await svc.worker.stop()
    assert done is not None and len(record) == 1

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    assert row.recovery_metadata is not None  # §6.3：恢复决策写入 Run recovery metadata
    assert row.recovery_metadata["mode"] == RECOVERY_FRESH
    assert row.recovery_metadata["checkpoint_state"] == CHECKPOINT_MISSING
    assert row.recovery_metadata["decided_at"]


@pytest.mark.asyncio
async def test_rc02_worker_missing_checkpoint_readonly_pending_retries(monkeypatch):
    """RC-02：无 Checkpoint + read_only 未决 -> 允许安全重试（不阻断）。"""
    record: list = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-rc02")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, "dead-worker", 30)
        run_id = run.id
        lease = _lease_for(run_id, version, worker_id="dead-worker")
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="call-ro", tool_name="calculator",
            args_snapshot={"expression": "1+1"}, lease=lease,
        )
        await ledger.mark_executing(rec.id, lease=lease)
        await ledger.mark_unknown(rec.id, error="进程崩溃", lease=lease)
        await s.commit()
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_COMPLETED})
    finally:
        await svc.worker.stop()
    assert done is not None and len(record) == 1  # 安全重试并完成

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    assert row.recovery_metadata["mode"] == RECOVERY_FRESH


# ════════════════════════════════════════════════════════════════════════════
# RC-03 / RC-04：无 Checkpoint 的未决副作用阻断重新推理（worker 集成）
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rc03_worker_missing_checkpoint_blocks_new_call_id(monkeypatch, contracts):
    """RC-03：无 Checkpoint + external_idempotent unknown -> 阻止重新生成新调用 ID。

    契约声明下游幂等的工具在 Checkpoint 丢失后同样禁止 fresh 重启 ——
    重新推理会产生新 tool_call_id / 新幂等键，无法复用原下游幂等键。
    """
    register_side_effect_contract(SideEffectContract(
        tool_name="order_api",
        side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
        downstream_idempotency_supported=True,
        idempotency_key_header="X-Idempotency-Key",
    ))
    record: list = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-rc03")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, "dead-worker", 30)
        run_id = run.id
        lease = _lease_for(run_id, version, worker_id="dead-worker")
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="call-A", tool_name="order_api",
            args_snapshot={"url": "https://api.example.com/orders"}, lease=lease,
        )
        await ledger.mark_executing(rec.id, lease=lease)
        await ledger.mark_unknown(rec.id, error="响应超时，远端结果未知", lease=lease)
        await s.commit()
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_NEEDS_RECONCILIATION})
    finally:
        await svc.worker.stop()
    assert done is not None
    assert record == []  # 绝不重新推理执行（不会生成新调用 ID）
    assert "禁止重新推理生成新调用 ID" in done.recovery_error

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    assert row.recovery_metadata["checkpoint_state"] == CHECKPOINT_MISSING
    assert row.recovery_metadata["pending"][0]["tool_call_id"] == "call-A"


@pytest.mark.asyncio
async def test_rc04_worker_missing_checkpoint_non_idempotent_reconciles(monkeypatch):
    """RC-04：无 Checkpoint + non_idempotent unknown -> needs_reconciliation。"""
    record: list = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-rc04")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, "dead-worker", 30)
        run_id = run.id
        lease = _lease_for(run_id, version, worker_id="dead-worker")
        ledger = ToolExecutionRepository(s)
        rec, _ = await ledger.begin(
            run_id=run_id, tool_call_id="call-B", tool_name="webhook_notify",
            args_snapshot={"url": "https://hooks.example.com/x"}, lease=lease,
        )
        await ledger.mark_executing(rec.id, lease=lease)
        await ledger.mark_unknown(rec.id, error="网络中断，远端结果未知", lease=lease)
        await s.commit()
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_NEEDS_RECONCILIATION})
    finally:
        await svc.worker.stop()
    assert done is not None
    assert record == []  # 未重新执行
    assert done.recovery_error and "禁止自动重放" in done.recovery_error

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    assert row.recovery_metadata["mode"] == RECOVERY_NEEDS_RECONCILIATION


# ════════════════════════════════════════════════════════════════════════════
# RC-05：Checkpoint 真实损坏 -> 安全阻断（真实文件损坏，不 mock）
# ════════════════════════════════════════════════════════════════════════════


async def _write_real_checkpoint(dsn: str, thread_id: str) -> None:
    """通过真实 AsyncSqliteSaver 写入一条可读的 Checkpoint。"""
    monkey_dsn = dsn
    original = settings.AGENT_CHECKPOINT_DSN
    settings.AGENT_CHECKPOINT_DSN = monkey_dsn
    try:
        cp = await CheckpointerFactory.get().get_checkpointer()
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        await cp.aput(
            config,
            empty_checkpoint(),
            {"source": "input", "step": -1, "writes": {}},
            {},
        )
    finally:
        settings.AGENT_CHECKPOINT_DSN = original


@pytest.mark.asyncio
async def test_rc05_corrupted_checkpoint_blob_blocks_fresh_restart(monkeypatch, tmp_path):
    """RC-05：Checkpoint 内容损坏（真实 BLOB 篡改）-> 不得自动回退 fresh。"""
    db_path = tmp_path / "cp.db"
    dsn = f"sqlite:///{db_path}".replace("\\", "/")
    thread_id = "agent-1-run-corrupt1"

    await _write_real_checkpoint(dsn, thread_id)

    # 真实损坏：篡改 checkpoint BLOB（反序列化必败）
    con = sqlite3.connect(str(db_path))
    con.execute(
        "UPDATE checkpoints SET checkpoint = X'DEADBEEFDEADBEEF' WHERE thread_id = ?",
        (thread_id,),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_DSN", dsn)
    svc = AgentRuntimeService()
    state = await svc.get_thread_state(thread_id)
    assert state is not None and state.get("error")  # 真实读取失败，不是 mock

    # 恢复决策：安全阻断 + recovery_error，绝不 fresh
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo, thread_id=thread_id)
        await repo.claim(run.id, "dead-worker", 30)
        run_id = run.id
        row = await repo.get_run(run_id)
        row.attempt = 2
        await s.commit()

        # 重新加载后在会话内调用（提交后实例属性过期）
        row = await repo.get_run(run_id)
        mode, reason, actions, metadata = await svc._recovery_check(row)

    assert mode == RECOVERY_NEEDS_RECONCILIATION  # 不是 RECOVERY_FRESH
    assert "安全阻断" in reason and "禁止 fresh restart" in reason
    assert metadata["checkpoint_state"] == CHECKPOINT_UNREADABLE

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    assert row.recovery_metadata["checkpoint_state"] == CHECKPOINT_UNREADABLE
    assert row.recovery_error and "安全阻断" in row.recovery_error


@pytest.mark.asyncio
async def test_rc05_missing_checkpoint_table_blocks_fresh_restart(monkeypatch, tmp_path):
    """RC-05 变体：Checkpoint 表被删除（数据库损坏）-> 同样安全阻断。"""
    db_path = tmp_path / "cp2.db"
    dsn = f"sqlite:///{db_path}".replace("\\", "/")
    thread_id = "agent-1-run-corrupt2"

    await _write_real_checkpoint(dsn, thread_id)

    con = sqlite3.connect(str(db_path))
    con.execute("DROP TABLE checkpoints")
    con.commit()
    con.close()

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_DSN", dsn)
    svc = AgentRuntimeService()
    state = await svc.get_thread_state(thread_id)
    assert state is not None and state.get("error")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo, thread_id=thread_id)
        await repo.claim(run.id, "dead-worker", 30)
        row = await repo.get_run(run.id)
        row.attempt = 2
        await s.commit()

        row = await repo.get_run(run.id)
        mode, reason, _, metadata = await svc._recovery_check(row)

    assert mode == RECOVERY_NEEDS_RECONCILIATION
    assert metadata["checkpoint_state"] == CHECKPOINT_UNREADABLE


@pytest.mark.asyncio
async def test_rc05_worker_flow_corrupted_checkpoint_records_recovery_error(monkeypatch, tmp_path):
    """RC-05 worker 全链路：损坏 Checkpoint 的 Run 被领取后进入 needs_reconciliation
    并记录 recovery_error / recovery_metadata，绝不 fresh 重新执行。"""
    db_path = tmp_path / "cp3.db"
    dsn = f"sqlite:///{db_path}".replace("\\", "/")
    thread_id = "agent-1-run-corrupt3"

    await _write_real_checkpoint(dsn, thread_id)
    con = sqlite3.connect(str(db_path))
    con.execute(
        "UPDATE checkpoints SET checkpoint = X'DEADBEEFDEADBEEF' WHERE thread_id = ?",
        (thread_id,),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_DSN", dsn)
    # 前置校验：损坏必须已经生效（真实读取失败，不是 mock）
    svc0 = AgentRuntimeService()
    st0 = await svc0.get_thread_state(thread_id)
    assert st0 is not None and st0.get("error"), f"损坏未生效: {st0}"

    record: list = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-rc05")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo, thread_id=thread_id)
        await repo.claim(run.id, "dead-worker", 30)
        await s.commit()
        run_id = run.id
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

    await svc.worker.start()
    try:
        done = await _wait_status(run_id, {STATUS_NEEDS_RECONCILIATION})
    finally:
        await svc.worker.stop()
    assert done is not None
    assert record == []  # 没有 fresh 重新执行
    assert done.recovery_error and "安全阻断" in done.recovery_error

    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    assert row.recovery_metadata["checkpoint_state"] == CHECKPOINT_UNREADABLE


# ════════════════════════════════════════════════════════════════════════════
# RC-06 / §6.2 C：Checkpoint 可读时重放复用原调用身份
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rc06_replay_reuses_original_tool_call_id_and_key(monkeypatch):
    """RC-06 / §6.2 C：已 succeeded 的调用重放返回缓存结果，
    复用原 tool_call_id 对应的同一幂等键，不重复执行。"""
    calls: list = []

    async def behavior(name, args):
        calls.append(args)
        return "订单已创建"

    monkeypatch.setattr(
        "app.services.agent_runtime.tools.adapter.ToolExecutor",
        _fake_executor(behavior),
    )
    run_id, lv = await _claimed_run()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=lv), {"webhook_notify"})
    out1 = await adapter._execute(
        "webhook_notify", {"tool_call_id": "call-A", "url": "https://hooks.example.com/x"}
    )
    assert out1 == "订单已创建"

    # 恢复重放：同一 tool_call_id -> 缓存结果，不重复执行
    adapter2 = _make_adapter(_make_ctx(run_id, lease_version=lv), {"webhook_notify"})
    out2 = await adapter2._execute(
        "webhook_notify", {"tool_call_id": "call-A", "url": "https://hooks.example.com/x"}
    )
    assert out2 == "订单已创建"
    assert len(calls) == 1  # 不重复执行

    async with AsyncSessionLocal() as s:
        ledger = ToolExecutionRepository(s)
        rec = await ledger.get(run_id, "call-A")
        # 同一 tool_call_id 只有一条记录：原调用身份与幂等键保持不变
        assert rec.status == TOOL_EXEC_SUCCEEDED
        assert rec.idempotency_key == hashlib.sha256(
            f"{run_id}:call-A".encode()
        ).hexdigest()


@pytest.mark.asyncio
async def test_undeclared_post_no_key_injection_and_unknown_blocks_replay(monkeypatch):
    """TL-09：不支持下游幂等的 POST 结果未知后阻止重放（默认契约行为）。"""
    calls: list = []

    async def behavior(name, args):
        calls.append(args)
        raise TimeoutError("响应超时，远端结果未知")

    monkeypatch.setattr(
        "app.services.agent_runtime.tools.adapter.ToolExecutor",
        _fake_executor(behavior),
    )
    monkeypatch.setattr(ToolPolicy, "check_url", lambda self, url: None)
    run_id, lv = await _claimed_run()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=lv), {"http_post"})
    out1 = await adapter._execute(
        "http_post",
        {"tool_call_id": "tc-nk", "url": "https://api.example.com/x", "body": {}},
    )
    assert "工具执行错误" in out1
    assert len(calls) == 1
    # 未声明下游幂等 -> 不注入 Idempotency-Key
    assert not (calls[0].get("headers") or {}).get("Idempotency-Key")

    adapter2 = _make_adapter(_make_ctx(run_id, lease_version=lv), {"http_post"})
    out2 = await adapter2._execute(
        "http_post",
        {"tool_call_id": "tc-nk", "url": "https://api.example.com/x", "body": {}},
    )
    assert len(calls) == 1  # 未再次 POST
    assert "阻止自动重放" in out2

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-nk")
    assert rec.status == "needs_reconciliation"


# ════════════════════════════════════════════════════════════════════════════
# §6.3：恢复决策审计（日志 + recovery_metadata）
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recovery_decision_logged_and_persisted(monkeypatch, caplog):
    """§6.3：恢复决策写入日志和 Run recovery_error / recovery metadata。"""
    record: list = []
    monkeypatch.setattr(AgentRuntimeService, "_drive", _stub_drive(record))
    svc = _make_service(worker_id="w-audit")

    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        await repo.claim(run.id, "dead-worker", 30)
        await s.commit()
        run_id = run.id
        row = await repo.get_run(run_id)
        row.lease_expires_at = row.lease_expires_at.replace(year=2000)
        await s.commit()

    with caplog.at_level(logging.INFO, logger="agent_runtime"):
        await svc.worker.start()
        try:
            done = await _wait_status(run_id, {STATUS_COMPLETED})
        finally:
            await svc.worker.stop()
    assert done is not None

    assert any(
        "恢复决策" in rec.message and "mode=fresh" in rec.message
        for rec in caplog.records
    )
    async with AsyncSessionLocal() as s:
        row = await RunRepository(s).get_run(run_id)
    meta = row.recovery_metadata
    assert meta["mode"] == RECOVERY_FRESH
    assert meta["checkpoint_state"] == CHECKPOINT_MISSING
    assert isinstance(meta["pending"], list)
    assert meta["reason"] and meta["decided_at"]

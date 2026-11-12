"""Checkpointer 持久化与多进程部署测试（P0-01，CP-01～CP-04）。

运行：
    pytest tests/test_checkpointer_persistence.py -q

真实服务依赖（不可达时自动 skip，测试报告中如实标注）：
- PostgreSQL Checkpointer：TEST_CHECKPOINT_PG_DSN
  默认 postgresql://ai_cp:123456@localhost:5433/agent_checkpoints
  即 docker/docker-compose.yml 中 checkpoint_pg 服务的宿主机映射端口
  （启动：docker compose -f docker/docker-compose.yml up -d checkpoint_pg）

场景对照（任务书 16.1）：
- CP-01 两个独立进程共享 PostgreSQL Checkpointer：状态互相可见
- CP-02 Worker A 写 Checkpoint 后被强制终止：Worker B 能从同一 Thread 恢复
- CP-03 Checkpointer DSN 错误 / 生产误配 Memory：启动失败，不降级 Memory
- CP-04 Checkpointer 连接关闭：无连接泄漏
（CP-05 同 Session 双进程执行权约束依赖 Session Lease，见 Batch 5）
"""
import asyncio
import os
import subprocess
import sys

import pytest
import pytest_asyncio

from app.core.config import Settings, settings
from app.services.agent_runtime.persistence import (
    CheckpointerFactory,
    mask_dsn,
    normalize_dsn,
)

PG_DSN = os.environ.get(
    "TEST_CHECKPOINT_PG_DSN",
    "postgresql://ai_cp:123456@localhost:5433/agent_checkpoints",
)
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程脚本（CP-01/CP-02）：独立进程用 CheckpointerFactory 写入 / 读取 PG
_CHILD_WRITE = """
import asyncio, os
from app.services.agent_runtime.persistence import CheckpointerFactory

THREAD = os.environ["CHILD_THREAD"]

async def main():
    factory = CheckpointerFactory()
    cp = await factory.get_checkpointer()
    config = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
    checkpoint = {
        "id": "ckpt-child-a",
        "v": 4,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"messages": ["hello-from-child-A"]},
        "channel_versions": {"messages": 1},
        "versions_seen": {},
    }
    await cp.aput(config, checkpoint, {"source": "test", "step": -1, "writes": {}}, {"messages": 1})
    print("WRITTEN", flush=True)
    if os.environ.get("CHILD_WAIT") == "1":
        # 模拟执行中途：阻塞等待被 SIGKILL（不做任何清理/关闭）
        await asyncio.Event().wait()
    else:
        await factory.aclose()

asyncio.run(main())
"""

_CHILD_READ = """
import asyncio, os
from app.services.agent_runtime.persistence import CheckpointerFactory

THREAD = os.environ["CHILD_THREAD"]

async def main():
    factory = CheckpointerFactory()
    cp = await factory.get_checkpointer()
    config = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
    tup = await cp.aget_tuple(config)
    if tup is None:
        print("MISSING", flush=True)
    else:
        msgs = tup.checkpoint.get("channel_values", {}).get("messages")
        print("READ:" + repr(msgs), flush=True)
    await factory.aclose()

asyncio.run(main())
"""


async def _pg_reachable(dsn: str) -> bool:
    try:
        from psycopg import AsyncConnection

        conn = await AsyncConnection.connect(dsn, autocommit=True, connect_timeout=3)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def pg_dsn(monkeypatch):
    """真实 PostgreSQL DSN；不可达时跳过（测试报告标注为 skipped）。"""
    if not await _pg_reachable(PG_DSN):
        pytest.skip(f"PostgreSQL Checkpointer 不可达，跳过真实多进程测试: {mask_dsn(PG_DSN)}")
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_DSN", PG_DSN)
    return PG_DSN


def _run_child(script: str, thread: str, *, wait: bool = False):
    env = os.environ.copy()
    env["AGENT_CHECKPOINT_DSN"] = PG_DSN
    env["AGENT_STORE_DSN"] = ""
    env["CHILD_THREAD"] = thread
    if wait:
        env["CHILD_WAIT"] = "1"
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# ── DSN 规范化 / 脱敏（P0-01-B 统一 DSN 规范）──────────────────────────────


def test_normalize_dsn_unifies_postgres_prefix_and_driver_marker():
    assert normalize_dsn("postgres://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert (
        normalize_dsn("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
    assert (
        normalize_dsn("postgres+psycopg2://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
    assert normalize_dsn("sqlite:///./data/x.db") == "sqlite:///./data/x.db"
    assert normalize_dsn("memory") == "memory"


def test_mask_dsn_hides_password():
    masked = mask_dsn("postgresql://ai_cp:123456@checkpoint_pg:5432/agent_checkpoints")
    assert "123456" not in masked
    assert "ai_cp" in masked and "***" in masked


# ── CP-03：生产禁 Memory / 初始化失败不降级 ─────────────────────────────────


def test_production_rejects_memory_checkpointer():
    bad = Settings(
        APP_ENV="production",
        AGENT_RUNTIME_ENABLED=True,
        AGENT_CHECKPOINT_DSN="memory",
    )
    with pytest.raises(RuntimeError, match="拒绝启动"):
        bad.validate_startup()

    empty = Settings(
        APP_ENV="production",
        AGENT_RUNTIME_ENABLED=True,
        AGENT_CHECKPOINT_DSN="",
    )
    with pytest.raises(RuntimeError):
        empty.validate_startup()

    # 非生产 / 生产但 Runtime 关闭 / 生产用持久化后端：允许
    Settings(APP_ENV="development", AGENT_CHECKPOINT_DSN="memory").validate_startup()
    Settings(
        APP_ENV="production", AGENT_RUNTIME_ENABLED=False, AGENT_CHECKPOINT_DSN="memory"
    ).validate_startup()
    Settings(
        APP_ENV="production",
        AGENT_CHECKPOINT_DSN="sqlite:///./data/agent_checkpoints.db",
    ).validate_startup()
    Settings(
        APP_ENV="production",
        AGENT_CHECKPOINT_DSN="postgresql://u:p@h:5432/db",
    ).validate_startup()


def test_init_failure_does_not_fallback_to_memory(monkeypatch):
    """CP-03：DSN 错误时抛错且不静默降级 Memory；异常信息不泄露密码。"""
    bad_dsn = "postgresql://ai_cp:topsecret@127.0.0.1:1/nonexistent"
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_DSN", bad_dsn)
    factory = CheckpointerFactory()

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(factory.get_checkpointer())

    assert factory._checkpointer is None, "初始化失败后不得持有任何实例（含 Memory 降级）"
    assert "topsecret" not in str(exc_info.value), "异常信息不得包含明文密码"


# ── 单飞行初始化 / 生命周期（CP-04）────────────────────────────────────────


def test_concurrent_init_creates_single_instance(tmp_path):
    dsn = f"sqlite:///{tmp_path}/single_flight.db"
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "AGENT_CHECKPOINT_DSN", dsn)
        factory = CheckpointerFactory()

        async def run():
            results = await asyncio.gather(
                *[factory.get_checkpointer() for _ in range(8)]
            )
            assert all(r is results[0] for r in results), "并发初始化必须只创建一个实例"
            await factory.aclose()

        asyncio.run(run())
    finally:
        monkey.undo()


def test_sqlite_persists_across_instances_and_closes_connection(tmp_path, monkeypatch):
    """CP-04 + 持久化：aclose 释放连接；新实例从同一文件读回 Checkpoint。"""
    dsn = f"sqlite:///{tmp_path}/persist.db"
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_DSN", dsn)
    thread = {"configurable": {"thread_id": "t-persist", "checkpoint_ns": ""}}
    checkpoint = {
        "id": "ckpt-1",
        "v": 4,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"messages": ["persisted"]},
        "channel_versions": {"messages": 1},
        "versions_seen": {},
    }

    async def write_and_close():
        factory = CheckpointerFactory()
        cp = await factory.get_checkpointer()
        await cp.aput(thread, checkpoint, {"source": "test", "step": -1, "writes": {}}, {"messages": 1})
        old_conn = factory._checkpointer_conn
        await factory.aclose()
        assert factory._checkpointer is None and factory._checkpointer_conn is None
        # 连接已关闭：继续使用必须报错（无连接泄漏）
        with pytest.raises(Exception):
            await old_conn.execute("SELECT 1")

    async def reopen_read():
        factory = CheckpointerFactory()
        cp = await factory.get_checkpointer()
        tup = await cp.aget_tuple(thread)
        assert tup is not None
        assert tup.checkpoint["channel_values"]["messages"] == ["persisted"]
        await factory.aclose()

    asyncio.run(write_and_close())
    asyncio.run(reopen_read())  # 新进程语义：新工厂实例从文件恢复


def test_dsn_change_closes_old_connection(tmp_path):
    dsn_a = f"sqlite:///{tmp_path}/a.db"
    dsn_b = f"sqlite:///{tmp_path}/b.db"

    async def run():
        factory = CheckpointerFactory()
        await factory.get_checkpointer()
        old_conn = factory._checkpointer_conn
        settings.AGENT_CHECKPOINT_DSN = dsn_b
        await factory.get_checkpointer()
        assert factory._dsn == normalize_dsn(dsn_b)
        # DSN 变更后旧连接必须关闭（无连接泄漏）
        with pytest.raises(Exception):
            await old_conn.execute("SELECT 1")
        await factory.aclose()

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "AGENT_CHECKPOINT_DSN", dsn_a)
        asyncio.run(run())
    finally:
        monkey.undo()


# ── CP-01 / CP-02：真实 PostgreSQL 多进程 ──────────────────────────────────


@pytest.mark.asyncio
async def test_cp01_two_processes_share_pg_checkpointer(pg_dsn):
    """CP-01：子进程 A 写入，主进程 B 用独立连接读取，状态互相可见。"""
    thread_id = f"cp01-{os.getpid()}"
    proc = _run_child(_CHILD_WRITE, thread_id)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0 and "WRITTEN" in out, f"子进程写入失败: {out} {err}"

    factory = CheckpointerFactory()
    cp = await factory.get_checkpointer()
    tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    assert tup is not None, "进程 B 必须能看到进程 A 写入的 Checkpoint"
    assert tup.checkpoint["channel_values"]["messages"] == ["hello-from-child-A"]
    await factory.aclose()


@pytest.mark.asyncio
async def test_cp02_worker_killed_then_worker_b_recovers(pg_dsn):
    """CP-02：Worker A 写入后被强制终止（SIGKILL），Worker B 从同一 Thread 恢复。"""
    thread_id = f"cp02-{os.getpid()}"
    proc = _run_child(_CHILD_WRITE, thread_id, wait=True)
    try:
        # 等待 A 写入完成（不退出，随后强杀）
        line = proc.stdout.readline()
        assert "WRITTEN" in line, f"子进程未写入成功: {line}"
    finally:
        proc.kill()  # 强制终止：不执行 aclose / 任何清理
        proc.wait(timeout=30)

    # Worker B：全新连接恢复同一 Thread
    factory = CheckpointerFactory()
    cp = await factory.get_checkpointer()
    tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    assert tup is not None, "Worker A 被强杀后 Worker B 必须能恢复该 Thread"
    assert tup.checkpoint["channel_values"]["messages"] == ["hello-from-child-A"]
    await factory.aclose()


@pytest.mark.asyncio
async def test_cp04_pg_connections_released_on_aclose(pg_dsn):
    """CP-04：aclose 后 PostgreSQL 连接真实关闭，无泄漏。"""
    factory = CheckpointerFactory()
    cp = await factory.get_checkpointer()
    assert cp is not None
    conn = factory._checkpointer_conn
    assert not conn.closed
    await factory.aclose()
    assert conn.closed, "aclose 后 psycopg 连接必须关闭"
    assert factory._checkpointer is None and factory._checkpointer_conn is None

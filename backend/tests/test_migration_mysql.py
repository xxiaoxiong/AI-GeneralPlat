"""MySQL 8 真实迁移测试（P0-06）。

运行：
    pytest tests/test_migration_mysql.py -q

真实 MySQL 8 服务（不可达 / 非 8.x 时自动 skip，测试报告中如实标注）：
- TEST_MYSQL_MIGRATE_SYNC_DSN
  默认 mysql+pymysql://root:123456@localhost:3307/ai_plat_migration_test

注意：测试会 DROP 并重建 DSN 中的数据库，库名必须以 ``_test`` 结尾，
防止误删真实业务库。
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

MYSQL_DSN = os.environ.get(
    "TEST_MYSQL_MIGRATE_SYNC_DSN",
    "mysql+pymysql://root:123456@localhost:3307/ai_plat_migration_test",
)
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _server_engine():
    """连接到服务器级（mysql 系统库），用于 CREATE / DROP DATABASE。"""
    url = make_url(MYSQL_DSN).set(database="mysql")
    return create_engine(url, connect_args={"connect_timeout": 5})


@pytest.fixture(scope="module")
def mysql_dsn():
    url = make_url(MYSQL_DSN)
    if not (url.database or "").endswith("_test"):
        pytest.skip(f"迁移测试库名必须以 _test 结尾: {url.database}")
    try:
        eng = _server_engine()
        with eng.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"MySQL 不可达，跳过真实迁移测试: {e}")
    finally:
        eng.dispose()
    if not version.startswith("8."):
        pytest.skip(f"MySQL 版本非 8.x（当前 {version}），跳过 MySQL 8 迁移测试")
    return MYSQL_DSN


def _reset_db():
    url = make_url(MYSQL_DSN)
    eng = _server_engine()
    with eng.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{url.database}`"))
        conn.execute(
            text(f"CREATE DATABASE `{url.database}` CHARACTER SET utf8mb4")
        )
    eng.dispose()


def _run_alembic(*args):
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = MYSQL_DSN
    env["DATABASE_URL"] = MYSQL_DSN.replace("mysql+pymysql://", "mysql+aiomysql://")
    env.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, f"alembic {args} failed:\n{r.stdout}\n{r.stderr}"


def _inspect():
    """读取库结构（在连接上下文内物化，避免连接关闭后继续查询）。"""
    eng = create_engine(MYSQL_DSN, connect_args={"connect_timeout": 5})
    try:
        with eng.connect() as conn:
            tables = {r[0] for r in conn.execute(text("SHOW TABLES")).fetchall()}
            cols_map = {}
            unique_map = {}
            for t in tables:
                cols_map[t] = {
                    r[0] for r in conn.execute(text(f"SHOW COLUMNS FROM `{t}`")).fetchall()
                }
                rows = conn.execute(
                    text(
                        "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME "
                        "FROM information_schema.STATISTICS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                        "AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_INDEX"
                    ),
                    {"t": t},
                ).fetchall()
                groups = {}
                for idx, _seq, col in rows:
                    groups.setdefault(idx, []).append(col)
                unique_map[t] = [tuple(v) for v in groups.values()]
            return tables, cols_map, unique_map
    finally:
        eng.dispose()


def _has_unique(unique_map, table, *cols):
    return any(set(u) == set(cols) for u in unique_map.get(table, []))


def test_fresh_mysql8_upgrades_to_head(mysql_dsn):
    """验收：全新 MySQL 8 数据库可以升级到 head，新增字段/唯一约束正确。"""
    _reset_db()
    _run_alembic("upgrade", "head")

    tables, cols_map, unique_map = _inspect()
    assert {"agent_runs", "agent_run_events", "agent_tool_executions",
            "agent_approvals", "session_result_outbox"} <= tables

    run_cols = cols_map.get("agent_runs", set())
    for col in (
        "request_hash", "input_payload", "agent_config_snapshot", "started_at",
        "worker_id", "lease_expires_at", "heartbeat_at", "attempt",
        "lease_version", "last_event_seq", "recovery_error", "idempotency_key",
    ):
        assert col in run_cols, f"agent_runs 缺少列 {col}"
    for col in ("action_id", "tool_call_id", "args_hash", "action_payload",
                "decision_status"):
        assert col in cols_map.get("agent_approvals", set()), f"agent_approvals 缺少列 {col}"
    assert "thread_id" in cols_map.get("agent_run_events", set())

    # P1-02（Batch 5）：Session 乐观锁 / 执行权列 + Outbox 结构
    sess_cols = cols_map.get("agent_sessions", set())
    for col in ("version", "active_run_id", "active_run_lease_expires_at"):
        assert col in sess_cols, f"agent_sessions 缺少列 {col}"
    for col in ("run_id", "session_id", "status", "attempts", "last_error"):
        assert col in cols_map.get("session_result_outbox", set()), \
            f"session_result_outbox 缺少列 {col}"
    assert _has_unique(unique_map, "session_result_outbox", "run_id")

    # 唯一约束（MySQL 保留约束语义，按列组合验证）
    assert _has_unique(unique_map, "agent_runs", "idempotency_key", "user_id")
    assert _has_unique(unique_map, "agent_run_events", "run_id", "sequence")
    assert _has_unique(unique_map, "agent_tool_executions", "run_id", "tool_call_id")


def test_pre_phase2_mysql8_upgrades_to_head(mysql_dsn):
    """验收：已有第二阶段之前的 MySQL 数据库可以升级到 head。

    先升级到 a1b2c3d4e5f6（第二阶段改造前的最后一个迁移），再升级到 head，
    模拟存量部署的升级路径。
    """
    _reset_db()
    _run_alembic("upgrade", "a1b2c3d4e5f6")

    tables, _, _ = _inspect()
    assert "agent_runs" not in tables, "a1b2c3d4e5f6 不应包含 Phase 2 的 agent_runs"

    _run_alembic("upgrade", "head")
    tables, cols_map, unique_map = _inspect()
    assert {"agent_runs", "agent_run_events", "agent_tool_executions"} <= tables
    assert "lease_version" in cols_map.get("agent_runs", set())
    assert _has_unique(unique_map, "agent_tool_executions", "run_id", "tool_call_id")

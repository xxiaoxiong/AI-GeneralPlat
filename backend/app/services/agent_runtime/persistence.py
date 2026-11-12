"""
持久化：LangGraph Checkpointer / Store 工厂与 thread_id 约定。

- AGENT_CHECKPOINT_DSN: memory | sqlite:///path | postgresql+asyncpg://...
  默认 sqlite（本地/测试）；生产建议 PostgreSQL（多实例可恢复）
- thread_id = f"agent-{agent_id}-session-{session_id}"（无会话时用 run_id）
- checkpoint 承载线程执行状态；store（AGENT_STORE_DSN）承载跨线程记忆
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from app.core.config import settings


class CheckpointerFactory:
    """进程级 Checkpointer 单例管理。"""

    _instance: Optional["CheckpointerFactory"] = None

    def __init__(self) -> None:
        self._checkpointer: Any = None
        self._store: Any = None
        self._dsn: Optional[str] = None

    @classmethod
    def get(cls) -> "CheckpointerFactory":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _current_dsn(self) -> str:
        return getattr(settings, "AGENT_CHECKPOINT_DSN", "memory") or "memory"

    async def get_checkpointer(self) -> Any:
        dsn = self._current_dsn()
        if self._checkpointer is not None and self._dsn == dsn:
            return self._checkpointer

        if dsn == "memory":
            from langgraph.checkpoint.memory import InMemorySaver

            self._checkpointer = InMemorySaver()
        elif dsn.startswith("sqlite"):
            # sqlite:///./data/agent_checkpoints.db
            path = dsn.split("///", 1)[-1] if "///" in dsn else "./data/agent_checkpoints.db"
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            import aiosqlite

            conn = await aiosqlite.connect(path)
            self._checkpointer = AsyncSqliteSaver(conn)
            await self._checkpointer.setup()
        elif dsn.startswith("postgres"):
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                from psycopg import AsyncConnection

                conn = await AsyncConnection.connect(dsn, autocommit=True)
                self._checkpointer = AsyncPostgresSaver(conn)
                await self._checkpointer.setup()
            except ImportError as e:
                raise RuntimeError(
                    "AGENT_CHECKPOINT_DSN 使用 postgres 但缺少依赖 "
                    "langgraph-checkpoint-postgres / psycopg，请安装后重试"
                ) from e
        else:
            raise ValueError(f"不支持的 AGENT_CHECKPOINT_DSN: {dsn}")

        self._dsn = dsn
        return self._checkpointer

    async def get_store(self) -> Optional[Any]:
        """跨线程长期记忆 Store（可选）。"""
        dsn = getattr(settings, "AGENT_STORE_DSN", "") or ""
        if not dsn:
            return None
        if self._store is not None:
            return self._store
        if dsn == "memory":
            from langgraph.store.memory import InMemoryStore

            self._store = InMemoryStore()
        elif dsn.startswith("postgres"):
            from langgraph.store.postgres.aio import AsyncPostgresStore
            from psycopg import AsyncConnection

            conn = await AsyncConnection.connect(dsn, autocommit=True)
            self._store = AsyncPostgresStore(conn)
            await self._store.setup()
        else:
            raise ValueError(f"不支持的 AGENT_STORE_DSN: {dsn}")
        return self._store


def make_thread_id(agent_id: int, session_id: Optional[int], run_id: str) -> str:
    if session_id is not None:
        return f"agent-{agent_id}-session-{session_id}"
    return f"agent-{agent_id}-run-{run_id}"


def make_run_config(thread_id: str, run_id: str, user_id: int) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "run_id": run_id,
            "user_id": user_id,
        },
        "run_id": run_id,
        "recursion_limit": 100,
    }

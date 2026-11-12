"""
持久化：LangGraph Checkpointer / Store 工厂与 thread_id 约定。

- AGENT_CHECKPOINT_DSN: memory | sqlite:///path | postgresql://user:pass@host:port/db
  默认 memory（仅开发/单测）；Docker 多 Worker / 生产必须使用 PostgreSQL（多进程共享）
- DSN 规范化：``postgres://`` 统一为 ``postgresql://``，并剥离 SQLAlchemy 驱动标记
  （``+asyncpg`` 等）——Checkpointer 直接使用 psycopg，不能携带驱动后缀
- 生命周期：``get_checkpointer()`` 并发初始化只会创建一个实例（单飞行）；
  ``aclose()`` / ``close()`` 释放连接；DSN 变更时先关闭旧连接再创建新连接；
  初始化失败绝不静默降级到 Memory（抛出异常，连接尽力释放）
- thread_id = f"agent-{agent_id}-session-{session_id}"（无会话时用 run_id）
- checkpoint 承载线程执行状态；store（AGENT_STORE_DSN）承载跨线程记忆
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any, Optional, Tuple

from app.core.config import settings

if sys.platform == "win32":
    # psycopg 3 不支持 Windows 默认的 ProactorEventLoop（async 连接报
    # InterfaceError），统一使用 Selector 策略；Linux 部署不受影响
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger("agent_runtime.persistence")

# DSN 中密码脱敏（启动日志 / 异常消息均不得出现明文密码）
_PASSWORD_RE = re.compile(r"://([^:/@\s]+):([^@\s]+)@")


def mask_dsn(dsn: str) -> str:
    """脱敏 DSN 中的密码部分，用于日志与异常信息。"""
    return _PASSWORD_RE.sub(r"://\1:***@", dsn or "")


def normalize_dsn(dsn: str) -> str:
    """规范化 Checkpoint DSN（P0-01：统一 psycopg 可用的 PostgreSQL URI）。

    - ``postgres://`` → ``postgresql://``
    - 剥离 SQLAlchemy 驱动标记：``postgresql+asyncpg://`` → ``postgresql://``
    """
    dsn = (dsn or "").strip()
    if "://" in dsn:
        scheme, rest = dsn.split("://", 1)
        for marker in ("+asyncpg", "+psycopg2", "+pg8000", "+psycopg"):
            scheme = scheme.replace(marker, "")
        if scheme == "postgres":
            scheme = "postgresql"
        dsn = f"{scheme}://{rest}"
    return dsn


class CheckpointerFactory:
    """进程级 Checkpointer 单例管理（含连接生命周期）。"""

    _instance: Optional["CheckpointerFactory"] = None

    def __init__(self) -> None:
        self._checkpointer: Any = None
        self._store: Any = None
        self._dsn: Optional[str] = None
        self._store_dsn: Optional[str] = None
        self._checkpointer_conn: Any = None   # sqlite(postgres) 底层连接，aclose 时释放
        self._store_conn: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._init_lock = asyncio.Lock()      # 单飞行：并发初始化只创建一个实例

    @classmethod
    def get(cls) -> "CheckpointerFactory":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_loop(self) -> None:
        """asyncio.Lock 与异步连接都绑定创建时的事件循环。

        检测到运行循环变化（如测试每用例一个新事件循环）时，丢弃旧循环
        绑定的资源并重建锁；旧连接不再复用也无法在新循环中关闭，由进程
        退出回收。生产部署为单进程单事件循环，不受影响。
        """
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._loop is not None:
            logger.info("事件循环变化，丢弃旧循环绑定的 Checkpointer/Store 资源")
            self._checkpointer = None
            self._checkpointer_conn = None
            self._dsn = None
            self._store = None
            self._store_conn = None
            self._store_dsn = None
        self._loop = loop
        self._init_lock = asyncio.Lock()

    def _current_dsn(self) -> str:
        return getattr(settings, "AGENT_CHECKPOINT_DSN", "memory") or "memory"

    # ── Checkpointer ─────────────────────────────────────────────────────

    async def get_checkpointer(self) -> Any:
        self._ensure_loop()
        dsn = normalize_dsn(self._current_dsn())
        async with self._init_lock:
            if self._checkpointer is not None and self._dsn == dsn:
                return self._checkpointer
            if self._checkpointer is not None and self._dsn != dsn:
                # DSN 变更：先关闭旧连接，避免连接泄漏（P0-01 生命周期要求）
                logger.info("AGENT_CHECKPOINT_DSN 变更，关闭旧 Checkpointer 连接")
                await self._close_checkpointer()

            self._checkpointer, self._checkpointer_conn = await self._create_checkpointer(dsn)
            self._dsn = dsn
            return self._checkpointer

    async def _create_checkpointer(self, dsn: str) -> Tuple[Any, Any]:
        """按 DSN 创建 Checkpointer；失败时释放已建连接并抛错（不降级 Memory）。"""
        conn: Any = None
        try:
            if dsn == "memory":
                from langgraph.checkpoint.memory import InMemorySaver

                logger.info("Checkpointer 后端: memory（仅限开发/单测，不支持多进程共享）")
                return InMemorySaver(), None

            if dsn.startswith("sqlite"):
                # sqlite:///./data/agent_checkpoints.db
                path = dsn.split("///", 1)[-1] if "///" in dsn else "./data/agent_checkpoints.db"
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
                import aiosqlite

                conn = await aiosqlite.connect(path)
                checkpointer = AsyncSqliteSaver(conn)
                await checkpointer.setup()
                logger.info("Checkpointer 后端: sqlite path=%s 健康", path)
                return checkpointer, conn

            if dsn.startswith("postgresql"):
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                from psycopg import AsyncConnection

                conn = await AsyncConnection.connect(dsn, autocommit=True)
                checkpointer = AsyncPostgresSaver(conn)
                await checkpointer.setup()
                logger.info("Checkpointer 后端: postgresql dsn=%s 健康", mask_dsn(dsn))
                return checkpointer, conn

            raise ValueError(f"不支持的 AGENT_CHECKPOINT_DSN: {mask_dsn(dsn)}")
        except ImportError as e:
            await self._safe_close_conn(conn)
            raise RuntimeError(
                "AGENT_CHECKPOINT_DSN 使用 postgres 但缺少依赖 "
                "langgraph-checkpoint-postgres / psycopg，请安装后重试"
            ) from e
        except Exception as e:
            # 初始化失败：释放已创建连接，抛错拒绝启动；绝不静默降级 Memory
            await self._safe_close_conn(conn)
            logger.error(
                "Checkpointer 初始化失败（拒绝降级 Memory）dsn=%s: %s",
                mask_dsn(dsn), e,
            )
            raise RuntimeError(
                f"Checkpointer 初始化失败（不会降级 Memory）dsn={mask_dsn(dsn)}: {e}"
            ) from e

    # ── Store（跨线程记忆，可选）─────────────────────────────────────────

    async def get_store(self) -> Optional[Any]:
        """跨线程长期记忆 Store（AGENT_STORE_DSN 为空则不启用）。"""
        self._ensure_loop()
        dsn = normalize_dsn(getattr(settings, "AGENT_STORE_DSN", "") or "")
        if not dsn:
            return None
        async with self._init_lock:
            if self._store is not None and self._store_dsn == dsn:
                return self._store
            if self._store is not None and self._store_dsn != dsn:
                await self._close_store()

            conn: Any = None
            try:
                if dsn == "memory":
                    from langgraph.store.memory import InMemoryStore

                    self._store = InMemoryStore()
                elif dsn.startswith("postgresql") or dsn.startswith("postgres"):
                    from langgraph.store.postgres.aio import AsyncPostgresStore
                    from psycopg import AsyncConnection

                    conn = await AsyncConnection.connect(dsn, autocommit=True)
                    self._store = AsyncPostgresStore(conn)
                    await self._store.setup()
                    self._store_conn = conn
                else:
                    raise ValueError(f"不支持的 AGENT_STORE_DSN: {mask_dsn(dsn)}")
            except Exception as e:
                await self._safe_close_conn(conn)
                logger.error("Store 初始化失败 dsn=%s: %s", mask_dsn(dsn), e)
                raise RuntimeError(
                    f"Store 初始化失败（不会降级 Memory）dsn={mask_dsn(dsn)}: {e}"
                ) from e
            self._store_dsn = dsn
            logger.info("Store 后端初始化完成 dsn=%s", mask_dsn(dsn))
            return self._store

    # ── 生命周期（P0-01：连接释放 / 无泄漏）──────────────────────────────

    async def aclose(self) -> None:
        """释放 Checkpointer 与 Store 连接（应用关闭时调用）。"""
        self._ensure_loop()
        async with self._init_lock:
            await self._close_checkpointer()
            await self._close_store()
        logger.info("CheckpointerFactory 连接已释放")

    def close(self) -> None:
        """同步关闭接口（无事件循环场景）；有事件循环请用 ``aclose()``。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
        else:
            loop.create_task(self.aclose())

    async def _close_checkpointer(self) -> None:
        await self._safe_close_conn(self._checkpointer_conn)
        self._checkpointer_conn = None
        self._checkpointer = None
        self._dsn = None

    async def _close_store(self) -> None:
        await self._safe_close_conn(self._store_conn)
        self._store_conn = None
        self._store = None
        self._store_dsn = None

    @staticmethod
    async def _safe_close_conn(conn: Any) -> None:
        if conn is None:
            return
        try:
            result = conn.close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - 关闭失败不阻塞上层
            logger.warning("Checkpointer 底层连接关闭异常", exc_info=True)


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

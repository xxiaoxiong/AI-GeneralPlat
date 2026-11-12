from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    # SQLite 并发写入需要更长的锁等待（MySQL/PostgreSQL 无此问题）
    connect_args={"timeout": 30} if _IS_SQLITE else {},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    if _IS_SQLITE:
        # WAL 模式：读写互不阻塞，避免并发会话在回滚日志模式下互锁
        # （journal_mode 持久化于库文件，仅需设置一次）
        async with engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    if settings.is_production:
        # 生产环境 Schema 由 Alembic 迁移管理（启动入口先执行 upgrade head）；
        # create_all 只会补新表、不会给已有表加字段，不能作为生产升级手段
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

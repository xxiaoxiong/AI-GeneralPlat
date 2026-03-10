"""
数据库连接管理 API — 支持用户配置外部数据库，用于智能体自然语言查询
"""
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from pydantic import BaseModel

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.database_connection import DatabaseConnection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/databases", tags=["databases"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class DatabaseConnectionCreate(BaseModel):
    name: str
    description: str = ""
    db_type: str = "mysql"  # mysql / postgresql / sqlite
    host: str = "localhost"
    port: int = 3306
    database: str
    username: str = ""
    password: str = ""
    extra_params: str = ""


class DatabaseConnectionUpdate(DatabaseConnectionCreate):
    pass


class NLQueryRequest(BaseModel):
    """自然语言查询请求"""
    connection_id: int
    question: str


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("")
async def list_connections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(DatabaseConnection)
        .where(DatabaseConnection.owner_id == current_user.id, DatabaseConnection.is_active == True)
        .order_by(DatabaseConnection.created_at.desc())
    )
    conns = res.scalars().all()
    return {"items": [_conn_to_dict(c) for c in conns], "total": len(conns)}


@router.post("")
async def create_connection(
    body: DatabaseConnectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = DatabaseConnection(
        name=body.name,
        description=body.description,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        database=body.database,
        username=body.username,
        password=body.password,
        extra_params=body.extra_params,
        owner_id=current_user.id,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return _conn_to_dict(conn)


@router.get("/{conn_id}")
async def get_connection(
    conn_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_or_404(db, conn_id, current_user.id)
    return _conn_to_dict(conn)


@router.put("/{conn_id}")
async def update_connection(
    conn_id: int,
    body: DatabaseConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_or_404(db, conn_id, current_user.id)
    for field in ("name", "description", "db_type", "host", "port",
                  "database", "username", "password", "extra_params"):
        setattr(conn, field, getattr(body, field))
    await db.commit()
    await db.refresh(conn)
    return _conn_to_dict(conn)


@router.delete("/{conn_id}")
async def delete_connection(
    conn_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_or_404(db, conn_id, current_user.id)
    conn.is_active = False
    await db.commit()
    return {"message": "已删除"}


# ── 测试连接 ──────────────────────────────────────────────────────────────────

@router.post("/{conn_id}/test")
async def test_connection(
    conn_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = await _get_or_404(db, conn_id, current_user.id)
    try:
        engine = _build_engine(conn)
        from sqlalchemy.ext.asyncio import AsyncSession as ExtSession, async_sessionmaker
        session_factory = async_sessionmaker(engine, class_=ExtSession, expire_on_commit=False)
        async with session_factory() as ext_session:
            result = await ext_session.execute(text("SELECT 1"))
            result.fetchone()
        await engine.dispose()

        conn.last_tested_at = datetime.utcnow()
        conn.last_test_ok = True
        await db.commit()
        return {"ok": True, "message": "连接成功"}
    except Exception as e:
        conn.last_tested_at = datetime.utcnow()
        conn.last_test_ok = False
        await db.commit()
        return {"ok": False, "message": f"连接失败: {str(e)}"}


# ── 查看表结构 ────────────────────────────────────────────────────────────────

@router.get("/{conn_id}/schema")
async def get_schema(
    conn_id: int,
    table: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取数据库的表列表或指定表的列信息"""
    conn = await _get_or_404(db, conn_id, current_user.id)
    try:
        engine = _build_engine(conn)
        from sqlalchemy.ext.asyncio import AsyncSession as ExtSession, async_sessionmaker
        session_factory = async_sessionmaker(engine, class_=ExtSession, expire_on_commit=False)
        async with session_factory() as ext_session:
            if not table:
                tables = await _list_tables(ext_session, conn.db_type)
                await engine.dispose()
                return {"tables": tables}
            else:
                columns = await _list_columns(ext_session, conn.db_type, table)
                await engine.dispose()
                return {"table": table, "columns": columns}
    except Exception as e:
        return {"error": str(e)}


# ── 执行 SQL 查询（只读）──────────────────────────────────────────────────────

@router.post("/{conn_id}/query")
async def execute_query(
    conn_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """执行只读 SQL 查询，返回结果"""
    conn = await _get_or_404(db, conn_id, current_user.id)
    sql = body.get("sql", "").strip()
    limit = min(int(body.get("limit", 100)), 500)

    if not sql:
        raise HTTPException(400, "SQL 不能为空")

    import re
    if not re.match(r"^\s*select\b", sql, re.IGNORECASE):
        raise HTTPException(400, "只允许 SELECT 查询")

    _DANGEROUS = re.compile(
        r'\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|EXEC)\b',
        re.IGNORECASE
    )
    if _DANGEROUS.search(sql):
        raise HTTPException(400, "SQL 中包含不允许的关键词")

    try:
        engine = _build_engine(conn)
        from sqlalchemy.ext.asyncio import AsyncSession as ExtSession, async_sessionmaker
        session_factory = async_sessionmaker(engine, class_=ExtSession, expire_on_commit=False)
        async with session_factory() as ext_session:
            result = await ext_session.execute(text(sql))
            rows = result.fetchmany(limit)
            cols = list(result.keys())
        await engine.dispose()

        data = [dict(zip(cols, [str(v) if v is not None else None for v in row])) for row in rows]
        return {"columns": cols, "rows": data, "total": len(data)}
    except Exception as e:
        raise HTTPException(400, f"查询错误: {str(e)}")


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _build_engine(conn: DatabaseConnection):
    """根据连接配置构建 SQLAlchemy 异步引擎"""
    from sqlalchemy.ext.asyncio import create_async_engine

    db_type = conn.db_type
    if db_type == "mysql":
        driver = "mysql+aiomysql"
        url = f"{driver}://{conn.username}:{conn.password}@{conn.host}:{conn.port}/{conn.database}"
    elif db_type == "postgresql":
        driver = "postgresql+asyncpg"
        url = f"{driver}://{conn.username}:{conn.password}@{conn.host}:{conn.port}/{conn.database}"
    elif db_type == "sqlite":
        url = f"sqlite+aiosqlite:///{conn.database}"
    else:
        raise ValueError(f"不支持的数据库类型: {db_type}")

    if conn.extra_params:
        url += f"?{conn.extra_params}"

    return create_async_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=3)


async def _get_or_404(db: AsyncSession, conn_id: int, user_id: int) -> DatabaseConnection:
    res = await db.execute(
        select(DatabaseConnection).where(
            DatabaseConnection.id == conn_id,
            DatabaseConnection.owner_id == user_id,
            DatabaseConnection.is_active == True,
        )
    )
    conn = res.scalar_one_or_none()
    if not conn:
        raise HTTPException(404, "数据库连接不存在")
    return conn


def _conn_to_dict(c: DatabaseConnection) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "db_type": c.db_type,
        "host": c.host,
        "port": c.port,
        "database": c.database,
        "username": c.username,
        "is_active": c.is_active,
        "last_tested_at": c.last_tested_at.isoformat() if c.last_tested_at else None,
        "last_test_ok": c.last_test_ok,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


async def _list_tables(session, db_type: str) -> list:
    if db_type == "mysql":
        result = await session.execute(text("SHOW TABLES"))
    elif db_type == "postgresql":
        result = await session.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
        ))
    else:
        result = await session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ))
    return [row[0] for row in result.fetchall()]


async def _list_columns(session, db_type: str, table: str) -> list:
    import re
    if not re.match(r'^[a-zA-Z_]\w*$', table):
        raise ValueError("表名不合法")
    if db_type == "mysql":
        result = await session.execute(text(f"DESCRIBE `{table}`"))
        return [{"name": r[0], "type": r[1], "nullable": r[2], "key": r[3]} for r in result.fetchall()]
    elif db_type == "postgresql":
        result = await session.execute(text(
            f"SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            f"WHERE table_name='{table}' ORDER BY ordinal_position"
        ))
        return [{"name": r[0], "type": r[1], "nullable": r[2]} for r in result.fetchall()]
    else:
        result = await session.execute(text(f"PRAGMA table_info('{table}')"))
        return [{"name": r[1], "type": r[2], "nullable": not r[3]} for r in result.fetchall()]

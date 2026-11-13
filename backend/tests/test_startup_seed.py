"""启动播种并发安全测试（Docker 多 Worker 启动门禁）。

背景：uvicorn --workers 2 时两个进程并发执行 ``_seed_initial_data``，
曾在 ``roles.name`` 唯一键上撞车导致首启失败（P0 门禁：Docker 启动流程须真实通过）。
修复：整段播种包单事务 + 捕获 IntegrityError 视为其他进程已完成播种。

运行：
    pytest backend/tests/test_startup_seed.py -q
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine, init_db
from app.main import _seed_initial_data
from app.models.app_market import AppTemplate
from app.models.user import Role, User, UserRole


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in (UserRole, User, Role, AppTemplate):
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


async def _count(model, *where) -> int:
    async with AsyncSessionLocal() as s:
        q = select(func.count()).select_from(model)
        for w in where:
            q = q.where(w)
        return (await s.execute(q)).scalar()


@pytest.mark.asyncio
async def test_concurrent_seed_single_winner_no_error():
    """两个进程并发播种：不得抛异常，admin 用户与角色恰各一份。"""
    await asyncio.gather(_seed_initial_data(), _seed_initial_data())
    assert await _count(User, User.username == settings.ADMIN_USERNAME) == 1
    assert await _count(Role, Role.name == "admin") == 1


@pytest.mark.asyncio
async def test_seed_is_idempotent():
    """已播种后重复执行直接返回，不报错、不重复。"""
    await _seed_initial_data()
    await _seed_initial_data()
    assert await _count(User) == 1
    assert await _count(UserRole) == 1

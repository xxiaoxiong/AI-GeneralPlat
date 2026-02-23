from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from app.core.database import get_db
from app.core.security import get_password_hash
from app.models.user import User, Role, UserRole
from app.schemas.user import UserCreate, UserUpdate, UserOut, UserWithRoles, RoleOut
from app.schemas.common import PageResponse
from app.api.deps import get_current_user, get_current_superuser
from app.services.audit import log_action

router = APIRouter(prefix="/users", tags=["用户管理"])


@router.get("", response_model=PageResponse[UserOut])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = None,
    is_active: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    query = select(User)
    if keyword:
        query = query.where(
            (User.username.contains(keyword)) |
            (User.email.contains(keyword)) |
            (User.full_name.contains(keyword))
        )
    if is_active is not None:
        query = query.where(User.is_active == is_active)

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(User.id.desc())
    result = await db.execute(query)
    users = result.scalars().all()

    return PageResponse(
        items=[UserOut.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("", response_model=UserOut)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.username == body.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="邮箱已存在")

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=get_password_hash(body.password),
        full_name=body.full_name,
        department=body.department,
        phone=body.phone,
    )
    db.add(user)
    await db.flush()
    await log_action(db, current_user.id, current_user.username, "create_user", "user",
                     resource_id=str(user.id))
    return UserOut.model_validate(user)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return UserOut.model_validate(user)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(user, field, value)
    db.add(user)
    await log_action(db, current_user.id, current_user.username, "update_user", "user",
                     resource_id=str(user_id))
    return UserOut.model_validate(user)


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    await db.delete(user)
    await log_action(db, current_user.id, current_user.username, "delete_user", "user",
                     resource_id=str(user_id))
    return {"message": "删除成功"}


@router.post("/{user_id}/roles/{role_id}")
async def assign_role(
    user_id: int,
    role_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.id == user_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="用户不存在")
    result = await db.execute(select(Role).where(Role.id == role_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="角色不存在")
    result = await db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="已拥有该角色")
    db.add(UserRole(user_id=user_id, role_id=role_id))
    return {"message": "角色分配成功"}


@router.delete("/{user_id}/roles/{role_id}")
async def remove_role(
    user_id: int,
    role_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
    )
    ur = result.scalar_one_or_none()
    if not ur:
        raise HTTPException(status_code=404, detail="用户未拥有该角色")
    await db.delete(ur)
    return {"message": "角色移除成功"}

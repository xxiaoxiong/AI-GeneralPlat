import secrets
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from app.core.database import get_db
from app.models.user import User
from app.models.app_market import AppTemplate, AppInstance
from app.schemas.app_market import (
    AppTemplateOut, AppInstanceCreate, AppInstanceUpdate, AppInstanceOut,
)
from app.schemas.common import PageResponse
from app.api.deps import get_current_user, get_current_superuser
from app.services.audit import log_action

router = APIRouter(prefix="/apps", tags=["应用市场"])


@router.get("/templates", response_model=PageResponse[AppTemplateOut])
async def list_templates(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = select(AppTemplate).where(AppTemplate.is_active == True)
    if category:
        query = query.where(AppTemplate.category == category)
    if keyword:
        query = query.where(
            (AppTemplate.display_name.contains(keyword)) |
            (AppTemplate.description.contains(keyword))
        )

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(
        AppTemplate.sort_order, AppTemplate.id
    )
    result = await db.execute(query)
    templates = result.scalars().all()

    return PageResponse(
        items=[AppTemplateOut.model_validate(t) for t in templates],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.get("/templates/{template_id}", response_model=AppTemplateOut)
async def get_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(AppTemplate).where(AppTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="应用模板不存在")
    return AppTemplateOut.model_validate(template)


@router.get("/instances", response_model=PageResponse[AppInstanceOut])
async def list_instances(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(AppInstance).where(AppInstance.owner_id == current_user.id)
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(AppInstance.id.desc())
    result = await db.execute(query)
    instances = result.scalars().all()

    return PageResponse(
        items=[AppInstanceOut.model_validate(i) for i in instances],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("/instances", response_model=AppInstanceOut)
async def create_instance(
    body: AppInstanceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(AppTemplate).where(AppTemplate.id == body.template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="应用模板不存在")

    template.usage_count = (template.usage_count or 0) + 1
    db.add(template)

    instance = AppInstance(
        **body.model_dump(),
        owner_id=current_user.id,
        access_token=secrets.token_urlsafe(32),
    )
    db.add(instance)
    await db.flush()
    await log_action(db, current_user.id, current_user.username, "create_app_instance", "app",
                     resource_id=str(instance.id))
    return AppInstanceOut.model_validate(instance)


@router.get("/instances/{instance_id}", response_model=AppInstanceOut)
async def get_instance(
    instance_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(AppInstance).where(AppInstance.id == instance_id))
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="应用实例不存在")
    return AppInstanceOut.model_validate(instance)


@router.put("/instances/{instance_id}", response_model=AppInstanceOut)
async def update_instance(
    instance_id: int,
    body: AppInstanceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(AppInstance).where(AppInstance.id == instance_id))
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="应用实例不存在")
    if instance.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="无权限修改")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(instance, field, value)
    db.add(instance)
    return AppInstanceOut.model_validate(instance)


@router.delete("/instances/{instance_id}")
async def delete_instance(
    instance_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(AppInstance).where(AppInstance.id == instance_id))
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="应用实例不存在")
    if instance.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="无权限删除")
    await db.delete(instance)
    return {"message": "删除成功"}

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from datetime import datetime

from app.core.database import get_db
from app.models.user import User
from app.models.audit import AuditLog
from app.schemas.common import PageResponse
from app.api.deps import get_current_superuser

router = APIRouter(prefix="/audit", tags=["审计日志"])


class AuditLogOut:
    pass


from pydantic import BaseModel


class AuditLogSchema(BaseModel):
    id: int
    user_id: Optional[int] = None
    username: Optional[str] = None
    action: str
    module: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    detail: Optional[dict] = None
    ip_address: Optional[str] = None
    status: str
    error_msg: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime
    model_config = {"from_attributes": True}


@router.get("", response_model=PageResponse[AuditLogSchema])
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    username: Optional[str] = None,
    action: Optional[str] = None,
    module: Optional[str] = None,
    status: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    query = select(AuditLog)
    if username:
        query = query.where(AuditLog.username.contains(username))
    if action:
        query = query.where(AuditLog.action == action)
    if module:
        query = query.where(AuditLog.module == module)
    if status:
        query = query.where(AuditLog.status == status)
    if start_time:
        query = query.where(AuditLog.created_at >= start_time)
    if end_time:
        query = query.where(AuditLog.created_at <= end_time)

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(AuditLog.id.desc())
    result = await db.execute(query)
    logs = result.scalars().all()

    return PageResponse(
        items=[AuditLogSchema.model_validate(log) for log in logs],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )

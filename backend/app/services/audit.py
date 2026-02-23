from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Dict, Any
from app.models.audit import AuditLog


async def log_action(
    db: AsyncSession,
    user_id: Optional[int],
    username: Optional[str],
    action: str,
    module: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    status: str = "success",
    error_msg: Optional[str] = None,
    duration_ms: Optional[int] = None,
):
    log = AuditLog(
        user_id=user_id,
        username=username,
        action=action,
        module=module,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else None,
        detail=detail,
        ip_address=ip,
        user_agent=user_agent,
        status=status,
        error_msg=error_msg,
        duration_ms=duration_ms,
    )
    db.add(log)

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional, AsyncGenerator
import json
import uuid
import os
from pathlib import Path

from app.core.database import get_db
from app.models.user import User
from app.models.model_provider import ModelProvider, ModelConfig
from app.schemas.model_provider import (
    ModelProviderCreate, ModelProviderUpdate, ModelProviderOut,
    ModelConfigCreate, ModelConfigUpdate, ModelConfigOut,
    ChatRequest, ModelTestRequest,
)
from app.schemas.common import PageResponse
from app.api.deps import get_current_user, get_current_superuser
from app.services.model_service import ModelService
from app.services.audit import log_action

router = APIRouter(prefix="/models", tags=["模型管理"])

IMAGE_UPLOAD_DIR = Path("uploads/images")
IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@router.post("/upload-image")
async def upload_image(
    file: UploadFile = File(...),
    _: "User" = Depends(get_current_user),
):
    """上传图片，返回可访问的 URL（多模态对话使用）"""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式: {file.content_type}")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "jpg"
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = IMAGE_UPLOAD_DIR / filename
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(status_code=400, detail="图片大小不能超过 10MB")
    with open(file_path, "wb") as f:
        f.write(content)
    return {"url": f"/api/v1/models/images/{filename}", "filename": filename}


@router.get("/images/{filename}")
async def get_image(filename: str):
    """获取已上传的图片"""
    from fastapi.responses import FileResponse
    file_path = IMAGE_UPLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(str(file_path))


# ─── Provider CRUD ────────────────────────────────────────────────────────────

@router.get("/providers", response_model=list[ModelProviderOut])
async def list_providers(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(ModelProvider).order_by(ModelProvider.id))
    return [ModelProviderOut.model_validate(p) for p in result.scalars().all()]


@router.post("/providers", response_model=ModelProviderOut)
async def create_provider(
    body: ModelProviderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    result = await db.execute(select(ModelProvider).where(ModelProvider.name == body.name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="供应商名称已存在")
    provider = ModelProvider(**body.model_dump())
    db.add(provider)
    await db.flush()
    await log_action(db, current_user.id, current_user.username, "create_provider", "model",
                     resource_id=str(provider.id))
    return ModelProviderOut.model_validate(provider)


@router.put("/providers/{provider_id}", response_model=ModelProviderOut)
async def update_provider(
    provider_id: int,
    body: ModelProviderUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(provider, field, value)
    db.add(provider)
    return ModelProviderOut.model_validate(provider)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    await db.delete(provider)
    return {"message": "删除成功"}


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    ok, msg = await ModelService.test_provider(provider)
    return {"success": ok, "message": msg}


# ─── Model Config CRUD ────────────────────────────────────────────────────────

@router.get("/configs", response_model=list[ModelConfigOut])
async def list_model_configs(
    provider_id: Optional[int] = None,
    model_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = select(ModelConfig)
    if provider_id:
        query = query.where(ModelConfig.provider_id == provider_id)
    if model_type:
        query = query.where(ModelConfig.model_type == model_type)
    result = await db.execute(query.order_by(ModelConfig.id))
    return [ModelConfigOut.model_validate(m) for m in result.scalars().all()]


@router.post("/configs", response_model=ModelConfigOut)
async def create_model_config(
    body: ModelConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    config = ModelConfig(**body.model_dump())
    db.add(config)
    await db.flush()
    return ModelConfigOut.model_validate(config)


@router.put("/configs/{config_id}", response_model=ModelConfigOut)
async def update_model_config(
    config_id: int,
    body: ModelConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(config, field, value)
    db.add(config)
    return ModelConfigOut.model_validate(config)


@router.delete("/configs/{config_id}")
async def delete_model_config(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    await db.delete(config)
    return {"message": "删除成功"}


# ─── Chat ─────────────────────────────────────────────────────────────────────

@router.post("/chat")
async def chat(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == body.model_config_id))
    model_config = result.scalar_one_or_none()
    if not model_config:
        raise HTTPException(status_code=404, detail="模型配置不存在")

    result = await db.execute(select(ModelProvider).where(ModelProvider.id == model_config.provider_id))
    provider = result.scalar_one_or_none()
    if not provider or not provider.is_active:
        raise HTTPException(status_code=400, detail="模型供应商不可用")

    if body.stream:
        async def event_stream() -> AsyncGenerator[str, None]:
            async for chunk in ModelService.chat_stream(provider, model_config, body):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    else:
        response = await ModelService.chat(provider, model_config, body)
        return response

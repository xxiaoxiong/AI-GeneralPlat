import re
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from app.core.database import get_db
from app.models.user import User
from app.models.prompt import PromptTemplate, PromptTemplateVersion
from app.schemas.prompt import (
    PromptTemplateCreate, PromptTemplateUpdate, PromptTemplateOut,
    PromptTemplateVersionOut, PromptRenderRequest, PromptRenderResponse,
    PromptDebugRequest,
)
from app.schemas.common import PageResponse
from app.api.deps import get_current_user

router = APIRouter(prefix="/prompts", tags=["提示词模板"])

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _render(content: str, variables: dict) -> tuple[str, list[str]]:
    missing = []
    def replacer(m):
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        missing.append(key)
        return m.group(0)
    rendered = _VAR_RE.sub(replacer, content)
    return rendered, list(set(missing))


@router.get("", response_model=PageResponse[PromptTemplateOut])
async def list_templates(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = None,
    category: Optional[str] = None,
    include_public: bool = True,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if include_public:
        query = select(PromptTemplate).where(
            (PromptTemplate.owner_id == current_user.id) | (PromptTemplate.is_public == True)
        )
    else:
        query = select(PromptTemplate).where(PromptTemplate.owner_id == current_user.id)

    if keyword:
        query = query.where(
            PromptTemplate.name.contains(keyword) | PromptTemplate.content.contains(keyword)
        )
    if category:
        query = query.where(PromptTemplate.category == category)

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(PromptTemplate.id.desc())
    result = await db.execute(query)
    items = result.scalars().all()

    return PageResponse(
        items=[PromptTemplateOut.model_validate(t) for t in items],
        total=total, page=page, page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("", response_model=PromptTemplateOut)
async def create_template(
    body: PromptTemplateCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Auto-detect variables from {{var}} patterns
    vars_found = list(set(_VAR_RE.findall(body.content)))
    tpl = PromptTemplate(
        **body.model_dump(exclude={"variables"}),
        variables=vars_found,
        owner_id=current_user.id,
        version=1,
    )
    db.add(tpl)
    await db.flush()
    # Save initial version
    ver = PromptTemplateVersion(
        template_id=tpl.id, version=1,
        content=body.content, variables=vars_found,
        change_note="初始版本", created_by=current_user.id,
    )
    db.add(ver)
    await db.commit()
    await db.refresh(tpl)
    return PromptTemplateOut.model_validate(tpl)


@router.get("/{template_id}", response_model=PromptTemplateOut)
async def get_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(PromptTemplate).where(PromptTemplate.id == template_id))
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.owner_id != current_user.id and not tpl.is_public:
        raise HTTPException(status_code=403, detail="无权访问")
    return PromptTemplateOut.model_validate(tpl)


@router.put("/{template_id}", response_model=PromptTemplateOut)
async def update_template(
    template_id: int,
    body: PromptTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(PromptTemplate).where(PromptTemplate.id == template_id))
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权修改")

    data = body.model_dump(exclude_none=True, exclude={"change_note"})
    if "content" in data:
        data["variables"] = list(set(_VAR_RE.findall(data["content"])))
        tpl.version += 1
        ver = PromptTemplateVersion(
            template_id=tpl.id, version=tpl.version,
            content=data["content"], variables=data["variables"],
            change_note=body.change_note or f"版本 {tpl.version}",
            created_by=current_user.id,
        )
        db.add(ver)

    for k, v in data.items():
        setattr(tpl, k, v)
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return PromptTemplateOut.model_validate(tpl)


@router.delete("/{template_id}")
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(PromptTemplate).where(PromptTemplate.id == template_id))
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权删除")
    await db.delete(tpl)
    await db.commit()
    return {"message": "删除成功"}


@router.get("/{template_id}/versions", response_model=list[PromptTemplateVersionOut])
async def list_versions(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(PromptTemplateVersion)
        .where(PromptTemplateVersion.template_id == template_id)
        .order_by(PromptTemplateVersion.version.desc())
    )
    return [PromptTemplateVersionOut.model_validate(v) for v in result.scalars().all()]


@router.post("/render", response_model=PromptRenderResponse)
async def render_prompt(
    body: PromptRenderRequest,
    _: User = Depends(get_current_user),
):
    rendered, missing = _render(body.content, body.variables)
    return PromptRenderResponse(rendered=rendered, missing_vars=missing)


@router.post("/debug")
async def debug_prompt(
    body: PromptDebugRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run rendered prompt against one or more models and return results."""
    from sqlalchemy import select as sa_select
    from app.models.model_provider import ModelConfig, ModelProvider
    from app.services.model_service import ModelService
    from app.schemas.model_provider import ChatRequest, ChatMessage

    rendered, missing = _render(body.content, body.variables)
    results = []

    for mc_id in body.model_config_ids:
        mc_result = await db.execute(sa_select(ModelConfig).where(ModelConfig.id == mc_id))
        mc = mc_result.scalar_one_or_none()
        if not mc:
            results.append({"model_config_id": mc_id, "error": "模型配置不存在"})
            continue

        prov_result = await db.execute(sa_select(ModelProvider).where(ModelProvider.id == mc.provider_id))
        provider = prov_result.scalar_one_or_none()
        if not provider:
            results.append({"model_config_id": mc_id, "error": "供应商不存在"})
            continue

        import time
        t0 = time.time()
        try:
            req = ChatRequest(
                model_config_id=mc_id,
                messages=[ChatMessage(role="user", content=rendered)],
                system_prompt=body.system_prompt or None,
                stream=False,
            )
            resp = await ModelService.chat(provider, mc, req)
            results.append({
                "model_config_id": mc_id,
                "model_name": mc.display_name,
                "content": resp.get("content", ""),
                "usage": resp.get("usage"),
                "duration_ms": int((time.time() - t0) * 1000),
            })
        except Exception as e:
            results.append({
                "model_config_id": mc_id,
                "model_name": mc.display_name,
                "error": str(e),
                "duration_ms": int((time.time() - t0) * 1000),
            })

    return {
        "rendered_prompt": rendered,
        "missing_vars": missing,
        "results": results,
    }

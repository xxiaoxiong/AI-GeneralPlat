import os
import uuid
import asyncio
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from pathlib import Path

from app.core.database import get_db
from app.core.config import settings
from app.models.user import User
from app.models.knowledge import KnowledgeBase, KnowledgeDocument
from app.schemas.knowledge import (
    KnowledgeBaseCreate, KnowledgeBaseUpdate, KnowledgeBaseOut,
    KnowledgeDocumentOut, SearchRequest, SearchResponse, SearchResult,
)
from app.schemas.common import PageResponse
from app.api.deps import get_current_user
from app.services.knowledge_service import KnowledgeService
from app.services.audit import log_action

router = APIRouter(prefix="/knowledge", tags=["知识库"])

ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "md", "markdown", "csv",
}


# ─── Knowledge Base CRUD ──────────────────────────────────────────────────────

@router.get("/bases", response_model=PageResponse[KnowledgeBaseOut])
async def list_knowledge_bases(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(KnowledgeBase).where(KnowledgeBase.owner_id == current_user.id)
    if keyword:
        query = query.where(KnowledgeBase.name.contains(keyword))

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(KnowledgeBase.id.desc())
    result = await db.execute(query)
    bases = result.scalars().all()

    return PageResponse(
        items=[KnowledgeBaseOut.model_validate(b) for b in bases],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("/bases", response_model=KnowledgeBaseOut)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    collection_name = f"kb_{current_user.id}_{uuid.uuid4().hex[:8]}"
    kb = KnowledgeBase(
        **body.model_dump(),
        owner_id=current_user.id,
        collection_name=collection_name,
    )
    db.add(kb)
    await db.flush()
    await log_action(db, current_user.id, current_user.username, "create_kb", "knowledge",
                     resource_id=str(kb.id))
    return KnowledgeBaseOut.model_validate(kb)


@router.get("/bases/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return KnowledgeBaseOut.model_validate(kb)


@router.put("/bases/{kb_id}", response_model=KnowledgeBaseOut)
async def update_knowledge_base(
    kb_id: int,
    body: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(kb, field, value)
    db.add(kb)
    return KnowledgeBaseOut.model_validate(kb)


@router.delete("/bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.collection_name:
        KnowledgeService.delete_collection(kb.collection_name)
    await db.delete(kb)
    return {"message": "删除成功"}


# ─── Documents ────────────────────────────────────────────────────────────────

@router.get("/bases/{kb_id}/documents", response_model=PageResponse[KnowledgeDocumentOut])
async def list_documents(
    kb_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="知识库不存在")

    query = select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb_id)
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(KnowledgeDocument.id.desc())
    result = await db.execute(query)
    docs = result.scalars().all()

    return PageResponse(
        items=[KnowledgeDocumentOut.model_validate(d) for d in docs],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("/bases/{kb_id}/documents", response_model=KnowledgeDocumentOut)
async def upload_document(
    kb_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    ext = Path(file.filename).suffix.lstrip(".").lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: .{ext}")

    file_size = 0
    upload_dir = Path(settings.UPLOAD_DIR) / str(kb_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}_{file.filename}"
    file_path = upload_dir / safe_name

    content = await file.read()
    file_size = len(content)
    if file_size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"文件大小超过限制 {settings.MAX_UPLOAD_SIZE_MB}MB")

    with open(file_path, "wb") as f:
        f.write(content)

    doc = KnowledgeDocument(
        knowledge_base_id=kb_id,
        filename=file.filename,
        file_path=str(file_path),
        file_type=ext,
        file_size=file_size,
        status="pending",
    )
    db.add(doc)
    kb.doc_count = (kb.doc_count or 0) + 1
    db.add(kb)
    await db.commit()
    await db.refresh(doc)
    doc_id = doc.id

    background_tasks.add_task(
        _process_document_bg,
        doc_id=doc_id,
        kb_id=kb_id,
        collection_name=kb.collection_name,
        file_path=str(file_path),
        file_type=ext,
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
    )

    return KnowledgeDocumentOut.model_validate(doc)


async def _process_document_bg(
    doc_id: int,
    kb_id: int,
    collection_name: str,
    file_path: str,
    file_type: str,
    chunk_size: int,
    chunk_overlap: int,
):
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
            doc = result.scalar_one_or_none()
            if not doc:
                return
            doc.status = "processing"
            db.add(doc)
            await db.commit()

            chunk_count = await KnowledgeService.index_document(
                collection_name=collection_name,
                document_id=doc_id,
                filename=doc.filename,
                file_path=file_path,
                file_type=file_type,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )

            doc.status = "done"
            doc.chunk_count = chunk_count
            db.add(doc)

            result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            kb = result.scalar_one_or_none()
            if kb:
                kb.vector_count = (kb.vector_count or 0) + chunk_count
                db.add(kb)

            await db.commit()
        except Exception as e:
            await db.rollback()
            async with AsyncSessionLocal() as db2:
                result = await db2.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
                doc = result.scalar_one_or_none()
                if doc:
                    doc.status = "failed"
                    doc.error_msg = str(e)
                    db2.add(doc)
                    await db2.commit()


@router.delete("/bases/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == doc_id,
            KnowledgeDocument.knowledge_base_id == kb_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if kb:
        KnowledgeService.delete_document_chunks(kb.collection_name, doc_id)
        kb.doc_count = max(0, (kb.doc_count or 1) - 1)
        kb.vector_count = max(0, (kb.vector_count or doc.chunk_count) - doc.chunk_count)
        db.add(kb)

    if os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    await db.delete(doc)
    return {"message": "删除成功"}


# ─── Web Crawl ────────────────────────────────────────────────────────────────

class WebCrawlRequest(BaseModel):
    url: str
    title: Optional[str] = None

@router.post("/bases/{kb_id}/crawl", response_model=KnowledgeDocumentOut)
async def crawl_webpage(
    kb_id: int,
    body: "WebCrawlRequest",
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """从网页 URL 爬取内容并加入知识库"""
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    from app.services.knowledge_service import KnowledgeService as KS
    text = await KS.extract_text_from_url(body.url)
    if text.startswith("[网页爬取失败"):
        raise HTTPException(status_code=400, detail=text)

    # 保存为临时文本文件
    upload_dir = Path(settings.UPLOAD_DIR) / str(kb_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}_webpage.txt"
    file_path = upload_dir / safe_name
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(f"来源: {body.url}\n\n{text}")

    display_name = body.title or body.url[:80]
    doc = KnowledgeDocument(
        knowledge_base_id=kb_id,
        filename=display_name,
        file_path=str(file_path),
        file_type="txt",
        file_size=len(text.encode("utf-8")),
        status="pending",
    )
    db.add(doc)
    kb.doc_count = (kb.doc_count or 0) + 1
    db.add(kb)
    await db.commit()
    await db.refresh(doc)

    background_tasks.add_task(
        _process_document_bg,
        doc_id=doc.id,
        kb_id=kb_id,
        collection_name=kb.collection_name,
        file_path=str(file_path),
        file_type="txt",
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
    )
    return KnowledgeDocumentOut.model_validate(doc)


# ─── Search ───────────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse)
async def search_knowledge(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    all_results = []
    for kb_id in body.knowledge_base_ids:
        result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        kb = result.scalar_one_or_none()
        if not kb or not kb.collection_name:
            continue
        items = await KnowledgeService.search(
            collection_name=kb.collection_name,
            query=body.query,
            top_k=body.top_k,
            score_threshold=body.score_threshold,
        )
        all_results.extend(items)

    all_results.sort(key=lambda x: x["score"], reverse=True)
    all_results = all_results[:body.top_k]

    return SearchResponse(
        results=[SearchResult(**r) for r in all_results],
        total=len(all_results),
        query=body.query,
    )

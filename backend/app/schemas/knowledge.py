from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    chunk_size: int = 500
    chunk_overlap: int = 50


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    is_active: Optional[bool] = None


class KnowledgeBaseOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    chunk_size: int
    chunk_overlap: int
    is_active: bool
    owner_id: int
    doc_count: int
    vector_count: int
    created_at: datetime
    model_config = {"from_attributes": True}


class KnowledgeDocumentOut(BaseModel):
    id: int
    knowledge_base_id: int
    filename: str
    file_type: str
    file_size: int
    status: str
    chunk_count: int
    error_msg: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}


class SearchRequest(BaseModel):
    query: str
    knowledge_base_ids: List[int]
    top_k: int = 5
    score_threshold: float = 0.5


class SearchResult(BaseModel):
    content: str
    score: float
    document_id: int
    filename: str
    chunk_index: int
    metadata: Optional[Dict[str, Any]] = None


class SearchResponse(BaseModel):
    results: List[SearchResult]
    total: int
    query: str

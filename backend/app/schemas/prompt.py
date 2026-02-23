from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel


class PromptTemplateCreate(BaseModel):
    name: str
    description: Optional[str] = None
    category: str = "general"
    content: str
    variables: List[str] = []
    is_public: bool = False
    tags: Optional[str] = None


class PromptTemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    content: Optional[str] = None
    variables: Optional[List[str]] = None
    is_public: Optional[bool] = None
    tags: Optional[str] = None
    change_note: Optional[str] = None


class PromptTemplateOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    category: str
    content: str
    variables: List[Any]
    is_public: bool
    owner_id: int
    version: int
    tags: Optional[str]
    usage_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PromptTemplateVersionOut(BaseModel):
    id: int
    template_id: int
    version: int
    content: str
    variables: List[Any]
    change_note: Optional[str]
    created_by: int
    created_at: datetime

    model_config = {"from_attributes": True}


class PromptRenderRequest(BaseModel):
    content: str
    variables: dict = {}


class PromptRenderResponse(BaseModel):
    rendered: str
    missing_vars: List[str] = []


class PromptDebugRequest(BaseModel):
    content: str
    variables: dict = {}
    model_config_ids: List[int] = []
    system_prompt: Optional[str] = None

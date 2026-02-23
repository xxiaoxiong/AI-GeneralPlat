from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class AppTemplateOut(BaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str] = None
    category: str
    icon: Optional[str] = None
    cover_image: Optional[str] = None
    app_type: str
    is_builtin: bool
    is_active: bool
    sort_order: int
    tags: Optional[str] = None
    usage_count: int
    created_at: datetime
    model_config = {"from_attributes": True}


class AppInstanceCreate(BaseModel):
    template_id: int
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    knowledge_base_id: Optional[int] = None
    model_config_id: Optional[int] = None
    workflow_id: Optional[int] = None
    is_public: bool = False


class AppInstanceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    knowledge_base_id: Optional[int] = None
    model_config_id: Optional[int] = None
    workflow_id: Optional[int] = None
    is_active: Optional[bool] = None
    is_public: Optional[bool] = None


class AppInstanceOut(BaseModel):
    id: int
    template_id: int
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    knowledge_base_id: Optional[int] = None
    model_config_id: Optional[int] = None
    workflow_id: Optional[int] = None
    owner_id: int
    is_active: bool
    is_public: bool
    access_token: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}

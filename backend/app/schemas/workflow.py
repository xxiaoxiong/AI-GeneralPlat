from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class WorkflowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    definition: Dict[str, Any] = {}
    trigger_type: str = "manual"
    trigger_config: Optional[Dict[str, Any]] = None
    category: Optional[str] = None
    icon: Optional[str] = None


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    is_published: Optional[bool] = None
    trigger_type: Optional[str] = None
    trigger_config: Optional[Dict[str, Any]] = None
    category: Optional[str] = None
    icon: Optional[str] = None


class WorkflowOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    definition: Dict[str, Any]
    is_active: bool
    is_published: bool
    version: int
    owner_id: int
    trigger_type: str
    category: Optional[str] = None
    icon: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class WorkflowExecuteRequest(BaseModel):
    input_data: Optional[Dict[str, Any]] = None


class WorkflowExecutionOut(BaseModel):
    id: int
    workflow_id: int
    status: str
    input_data: Optional[Dict[str, Any]] = None
    output_data: Optional[Dict[str, Any]] = None
    node_results: Optional[Dict[str, Any]] = None
    error_msg: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime
    model_config = {"from_attributes": True}

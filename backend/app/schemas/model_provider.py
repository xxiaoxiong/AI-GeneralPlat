from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class ModelProviderCreate(BaseModel):
    name: str
    display_name: str
    provider_type: str  # openai_compatible / ollama / custom
    base_url: str
    api_key: Optional[str] = None
    description: Optional[str] = None
    logo_url: Optional[str] = None
    extra_config: Optional[Dict[str, Any]] = None


class ModelProviderUpdate(BaseModel):
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    description: Optional[str] = None
    extra_config: Optional[Dict[str, Any]] = None


class ModelProviderOut(BaseModel):
    id: int
    name: str
    display_name: str
    provider_type: str
    base_url: str
    is_active: bool
    is_default: bool
    description: Optional[str] = None
    logo_url: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}


class ModelConfigCreate(BaseModel):
    provider_id: int
    name: str
    display_name: str
    model_type: str  # chat / embedding / image / audio
    context_length: int = 4096
    max_tokens: int = 2048
    input_price: float = 0.0
    output_price: float = 0.0
    supports_streaming: bool = True
    supports_function_call: bool = False
    description: Optional[str] = None
    extra_params: Optional[Dict[str, Any]] = None


class ModelConfigUpdate(BaseModel):
    provider_id: Optional[int] = None
    display_name: Optional[str] = None
    model_type: Optional[str] = None
    context_length: Optional[int] = None
    max_tokens: Optional[int] = None
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    is_active: Optional[bool] = None
    supports_streaming: Optional[bool] = None
    description: Optional[str] = None
    extra_params: Optional[Dict[str, Any]] = None


class ModelConfigOut(BaseModel):
    id: int
    provider_id: int
    name: str
    display_name: str
    model_type: str
    context_length: int
    max_tokens: int
    input_price: float
    output_price: float
    is_active: bool
    supports_streaming: bool
    supports_function_call: bool
    description: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}


class ChatMessage(BaseModel):
    role: str  # system / user / assistant
    content: str
    image_urls: Optional[List[str]] = None  # 多模态图片 URL 列表


class ChatRequest(BaseModel):
    model_config_id: int
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    stream: bool = True
    knowledge_base_id: Optional[int] = None
    system_prompt: Optional[str] = None


class ModelTestRequest(BaseModel):
    provider_id: int
    model_name: str
    prompt: str = "你好，请简单介绍一下你自己。"

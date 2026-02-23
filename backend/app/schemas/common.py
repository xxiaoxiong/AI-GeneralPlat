from pydantic import BaseModel
from typing import Optional, List, TypeVar, Generic, Any

T = TypeVar("T")


class PageResponse(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int
    page_size: int
    total_pages: int


class Response(BaseModel, Generic[T]):
    code: int = 200
    message: str = "success"
    data: Optional[T] = None


class ErrorResponse(BaseModel):
    code: int
    message: str
    detail: Optional[Any] = None

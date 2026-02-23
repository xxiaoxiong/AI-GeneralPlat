from sqlalchemy import Column, Integer, String, Text, Boolean, JSON, ForeignKey
from app.models.base import BaseModel


class PromptTemplate(BaseModel):
    __tablename__ = "prompt_templates"

    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(50), default="general")
    content = Column(Text, nullable=False)
    variables = Column(JSON, default=list)
    is_public = Column(Boolean, default=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    version = Column(Integer, default=1)
    tags = Column(String(200), nullable=True)
    usage_count = Column(Integer, default=0)


class PromptTemplateVersion(BaseModel):
    __tablename__ = "prompt_template_versions"

    template_id = Column(Integer, ForeignKey("prompt_templates.id"), nullable=False)
    version = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    variables = Column(JSON, default=list)
    change_note = Column(String(200), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)

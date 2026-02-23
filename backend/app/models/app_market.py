from sqlalchemy import String, Boolean, Text, Integer, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import BaseModel


class AppTemplate(BaseModel):
    __tablename__ = "app_templates"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)  # legal / finance / hr / sales / ops
    icon: Mapped[str] = mapped_column(String(100), nullable=True)
    cover_image: Mapped[str] = mapped_column(String(500), nullable=True)
    app_type: Mapped[str] = mapped_column(String(50), nullable=False)  # chat / workflow / form
    config_schema: Mapped[dict] = mapped_column(JSON, nullable=True)   # configurable fields schema
    default_config: Mapped[dict] = mapped_column(JSON, nullable=True)
    workflow_definition: Mapped[dict] = mapped_column(JSON, nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str] = mapped_column(String(500), nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)

    instances: Mapped[list["AppInstance"]] = relationship("AppInstance", back_populates="template")


class AppInstance(BaseModel):
    __tablename__ = "app_instances"

    template_id: Mapped[int] = mapped_column(Integer, ForeignKey("app_templates.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=True)
    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=True)
    model_config_id: Mapped[int] = mapped_column(Integer, nullable=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    access_token: Mapped[str] = mapped_column(String(100), nullable=True, unique=True)

    template: Mapped["AppTemplate"] = relationship("AppTemplate", back_populates="instances")

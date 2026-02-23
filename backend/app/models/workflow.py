from sqlalchemy import String, Boolean, Text, Integer, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import BaseModel


class Workflow(BaseModel):
    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    definition: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # nodes + edges JSON
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(50), default="manual")  # manual / schedule / webhook
    trigger_config: Mapped[dict] = mapped_column(JSON, nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=True)
    icon: Mapped[str] = mapped_column(String(100), nullable=True)

    executions: Mapped[list["WorkflowExecution"]] = relationship("WorkflowExecution", back_populates="workflow")


class WorkflowExecution(BaseModel):
    __tablename__ = "workflow_executions"

    workflow_id: Mapped[int] = mapped_column(Integer, ForeignKey("workflows.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / running / success / failed
    trigger_by: Mapped[int] = mapped_column(Integer, nullable=True)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=True)
    output_data: Mapped[dict] = mapped_column(JSON, nullable=True)
    node_results: Mapped[dict] = mapped_column(JSON, nullable=True)
    error_msg: Mapped[str] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=True)

    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="executions")

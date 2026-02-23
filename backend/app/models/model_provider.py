from sqlalchemy import String, Boolean, Text, Integer, JSON, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import BaseModel


class ModelProvider(BaseModel):
    __tablename__ = "model_providers"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False)  # openai_compatible / ollama / custom
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    logo_url: Mapped[str] = mapped_column(String(500), nullable=True)
    extra_config: Mapped[dict] = mapped_column(JSON, nullable=True)

    models: Mapped[list["ModelConfig"]] = relationship("ModelConfig", back_populates="provider")


class ModelConfig(BaseModel):
    __tablename__ = "model_configs"

    provider_id: Mapped[int] = mapped_column(Integer, ForeignKey("model_providers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_type: Mapped[str] = mapped_column(String(50), nullable=False)  # chat / embedding / image / audio
    context_length: Mapped[int] = mapped_column(Integer, default=4096)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    input_price: Mapped[float] = mapped_column(Float, default=0.0)   # per 1K tokens
    output_price: Mapped[float] = mapped_column(Float, default=0.0)  # per 1K tokens
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_streaming: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_function_call: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    extra_params: Mapped[dict] = mapped_column(JSON, nullable=True)

    provider: Mapped["ModelProvider"] = relationship("ModelProvider", back_populates="models")

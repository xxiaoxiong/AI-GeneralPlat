"""用户自定义数据库连接配置模型"""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey
from app.core.database import Base
from datetime import datetime


class DatabaseConnection(Base):
    """用户配置的外部数据库连接"""
    __tablename__ = "database_connections"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")

    # 连接参数
    db_type = Column(String(20), nullable=False)  # mysql / postgresql / sqlite
    host = Column(String(255), default="localhost")
    port = Column(Integer, default=3306)
    database = Column(String(255), nullable=False)
    username = Column(String(255), default="")
    password = Column(String(255), default="")  # 生产环境应加密存储
    extra_params = Column(Text, default="")  # 额外连接参数，如 charset=utf8mb4

    # 状态
    is_active = Column(Boolean, default=True)
    last_tested_at = Column(DateTime, nullable=True)
    last_test_ok = Column(Boolean, default=False)

    # 归属
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

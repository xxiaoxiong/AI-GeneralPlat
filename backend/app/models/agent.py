from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON
from app.models.base import Base


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    icon = Column(String(10), default="🤖")

    # 关联模型配置
    model_config_id = Column(Integer, ForeignKey("model_configs.id"), nullable=True)

    # 系统提示词
    system_prompt = Column(Text, default="你是一个智能助手，可以使用工具来帮助用户解决问题。")

    # ReAct 配置
    max_iterations = Column(Integer, default=10)       # 最大循环次数
    max_tokens = Column(Integer, default=4096)
    temperature = Column(String(10), default="0.7")

    # 工具列表（JSON 数组，存储工具配置）
    tools = Column(JSON, default=list)

    # 知识库关联（可选）
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True)

    # 数据库连接关联（可选，用于自然语言查询外部数据库）
    database_connection_id = Column(Integer, ForeignKey("database_connections.id"), nullable=True)

    is_active = Column(Boolean, default=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentSession(Base):
    """Agent 对话会话"""
    __tablename__ = "agent_sessions"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    title = Column(String(200), default="新对话")

    # 完整对话历史（含 ReAct 思考链）
    messages = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

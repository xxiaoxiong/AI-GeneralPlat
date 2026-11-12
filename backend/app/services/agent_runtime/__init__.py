"""
agent_runtime：基于 DeepAgents + LangChain + LangGraph 的 Agent 执行内核。

平台唯一 Agent 执行入口 = AgentRuntimeService（service.get_runtime_service）。
"""
from .service import AgentRuntimeService, get_runtime_service
from .context import RunScope, ExecutionBudget, RuntimeContext
from .events import RuntimeEvent, LegacyEventAdapter

__all__ = [
    "AgentRuntimeService",
    "get_runtime_service",
    "RunScope",
    "ExecutionBudget",
    "RuntimeContext",
    "RuntimeEvent",
    "LegacyEventAdapter",
]

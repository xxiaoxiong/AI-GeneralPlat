"""
运行上下文：执行身份、预算与运行期配置。

scope 一律由后端鉴权推导，不允许客户端自报用户身份。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class RunScope:
    """运行身份与资源作用域（由鉴权层构造）"""

    user_id: int
    tenant_id: Optional[str] = None  # 当前项目无租户概念时为 None，等价用户作用域

    def check_agent_access(self, owner_id: Optional[int]) -> bool:
        """Agent 可见性：所有者或平台公开（owner_id 为空）"""
        if owner_id is None:
            return True
        return owner_id == self.user_id

    def check_session_access(self, session_user_id: Optional[int]) -> bool:
        return session_user_id is None or session_user_id == self.user_id


@dataclass
class ExecutionBudget:
    """执行预算：次数 / 时长限制（token 预算留接口，暂按次数/时长执行）"""

    max_steps: int = 20                    # 图执行最大步数（模型调用轮数）
    max_tool_calls: int = 30               # 工具调用总次数
    max_model_calls: int = 30              # 模型调用次数
    run_timeout_seconds: float = 300.0     # 单次运行墙钟超时
    max_subagent_depth: int = 2            # 子代理派生深度上限
    max_tokens_budget: Optional[int] = None  # 预留：token 预算

    def exhausted_reason(self, steps: int, tool_calls: int, model_calls: int) -> Optional[str]:
        if model_calls > self.max_model_calls:
            return f"模型调用次数超过预算({self.max_model_calls})"
        if steps > self.max_steps:
            return f"执行步数超过预算({self.max_steps})"
        if tool_calls > self.max_tool_calls:
            return f"工具调用次数超过预算({self.max_tool_calls})"
        return None


@dataclass
class RuntimeContext:
    """一次运行的完整执行上下文（工厂 / 工具 / 服务共享）"""

    scope: RunScope
    run_id: str
    thread_id: str
    agent_id: int
    session_id: Optional[int] = None
    trace_id: str = ""
    model_id: Optional[str] = None  # 模型配置名（可观测字段）

    # 业务注入（来自平台层）
    agent_config: Dict[str, Any] = field(default_factory=dict)
    db_info: Optional[Dict[str, Any]] = None
    memory_context: str = ""

    budget: ExecutionBudget = field(default_factory=ExecutionBudget)

    # 运行期计数（由工具适配层 / 服务层维护）
    started_at: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    model_calls: int = 0
    steps: int = 0

    # 安全策略开关
    enable_hitl: bool = True
    sandbox_mode: str = "restricted"      # restricted / disabled / external
    allow_network: bool = True

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def to_log_fields(self) -> Dict[str, Any]:
        return {
            "request_id": self.run_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "tenant_id": self.scope.tenant_id,
            "user_id": self.scope.user_id,
            "session_id": self.session_id,
            "model_id": self.model_id,
        }


def messages_to_langchain(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把平台消息格式（role/content dict）转为 LangChain 消息 dict。

    旧会话历史中的 steps / ReAct 中间产物不进入模型上下文，
    assistant 消息只保留最终回答文本。
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
    )

    out = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            if not content.strip():
                continue
            out.append(AIMessage(content=content))
        elif role == "user":
            out.append(HumanMessage(content=content))
    return out

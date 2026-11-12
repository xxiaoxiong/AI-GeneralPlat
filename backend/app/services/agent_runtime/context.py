"""
运行上下文：执行身份、预算与运行期配置。

scope 一律由后端鉴权推导，不允许客户端自报用户身份。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler


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

    # Worker 身份与 Fencing Token（工具账本执行权标记，Phase 2/3）
    worker_id: Optional[str] = None
    lease_version: int = 0

    budget: ExecutionBudget = field(default_factory=ExecutionBudget)

    # 运行期计数（由工具适配层 / 服务层维护）
    started_at: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    # FR-10 统计口径：
    # - model_calls：一次 ChatModel/LLM 请求（ModelUsageCallback 按请求计数，不按 Chunk）
    # - model_node_runs：主图 model 节点执行次数（= agent 轮次，steps 递增位置）
    # - tokens_generated：生成 Token 数（usage 元数据优先，缺省按流式 token 事件计）
    model_calls: int = 0
    model_node_runs: int = 0
    tokens_generated: int = 0
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


def stable_message_id(message: Dict[str, Any], index: int, namespace: str = "") -> str:
    """平台消息稳定 ID（FR-08 方案 B）。

    会话历史为追加式：同一条消息在后续每轮注入、重试、恢复时派生出相同 ID，
    配合 LangGraph messages reducer 的"按 ID 替换/去重"语义，重复注入不会
    被重复追加。平台消息若已带 ``id``（历史数据升级后写回的图内消息 ID）则沿用。
    """
    if message.get("id"):
        return str(message["id"])
    role = str(message.get("role") or "")
    content = str(message.get("content") or "")
    digest = hashlib.sha256(
        f"{namespace}|{index}|{role}|{content}".encode("utf-8")
    ).hexdigest()
    return f"m-{digest[:20]}"


def messages_to_langchain(
    messages: List[Dict[str, Any]],
    *,
    namespace: str = "",
    reset: bool = True,
) -> List[Any]:
    """把平台消息格式（role/content dict）转为 LangChain 消息，携带稳定消息 ID。

    旧会话历史中的 steps / ReAct 中间产物不进入模型上下文，
    assistant 消息只保留最终回答文本。

    FR-08 主策略（方案 B：稳定消息 ID 去重）+ 确定性重播种：
    - 每条消息携带稳定 ID（``stable_message_id``），依赖 messages reducer 的
      ID 去重/替换语义，重试与恢复注入同一历史不会重复追加；
    - ``reset=True`` 时负载以 ``RemoveMessage(REMOVE_ALL_MESSAGES)`` 开头，
      以 Session 历史为唯一事实来源确定性重建消息通道基线——解决两类身份
      无法对齐的问题：Checkpoint 内模型生成消息（随机 UUID）与 Session 副本、
      升级前旧线程消息（历史兼容，§FR-08.4）。执行增量（工具轨迹、模型消息）
      在本轮执行内正常累积；挂起恢复（Command(resume) / payload=None）不重播种。
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        RemoveMessage,
        SystemMessage,
    )
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    out: List[Any] = []
    if reset:
        out.append(RemoveMessage(id=REMOVE_ALL_MESSAGES))
    for i, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content") or ""
        mid = stable_message_id(m, i, namespace)
        if role == "system":
            out.append(SystemMessage(content=content, id=mid))
        elif role == "assistant":
            if not content.strip():
                continue
            out.append(AIMessage(content=content, id=mid))
        elif role == "user":
            out.append(HumanMessage(content=content, id=mid))
    return out


def _usage_output_tokens(response: Any) -> Optional[int]:
    """从 LangChain 响应对象提取生成 token 数（usage 元数据，各家 SDK 适配）。"""
    try:
        gens = getattr(response, "generations", None) or []
        if gens and gens[0]:
            msg = getattr(gens[0][0], "message", None)
            usage = getattr(msg, "usage_metadata", None) or {}
            if usage.get("output_tokens") is not None:
                return int(usage["output_tokens"])
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("usage") or llm_output.get("token_usage") or {}
        for key in ("output_tokens", "completion_tokens"):
            if usage.get(key) is not None:
                return int(usage[key])
    except Exception:
        pass
    return None


class ModelUsageCallback(BaseCallbackHandler):
    """FR-10：模型调用预算与统计的统一计数（Runnable 生命周期回调）。

    口径：
    - model_calls：一次 ChatModel/LLM 请求计一次（on_chat_model_start /
      on_llm_start），流式多 Chunk 只计一次——不再按 Chunk 重复计数；
    - tokens_generated：生成 Token 数——响应 usage 元数据优先，无 usage 时
      按 on_llm_new_token 流式事件计数。
    同一请求两个钩子共用 run_id，按 run_id 去重防止重复计数。
    """

    def __init__(self, ctx: "RuntimeContext") -> None:
        self.ctx = ctx
        self._counted: set = set()
        self._streamed: Dict[Any, int] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):  # noqa: ANN001
        self._count(run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):  # noqa: ANN001
        self._count(run_id)

    def on_llm_new_token(self, token, *, chunk=None, run_id=None, **kwargs):  # noqa: ANN001
        if run_id is not None:
            self._streamed[run_id] = self._streamed.get(run_id, 0) + 1

    def on_chat_model_end(self, response, *, run_id, **kwargs):  # noqa: ANN001
        self._finish(run_id, response)

    def on_llm_end(self, response, *, run_id, **kwargs):  # noqa: ANN001
        self._finish(run_id, response)

    def _count(self, run_id: Any) -> None:
        if run_id in self._counted:
            return
        self._counted.add(run_id)
        self.ctx.model_calls += 1

    def _finish(self, run_id: Any, response: Any) -> None:
        streamed = self._streamed.pop(run_id, 0)
        used = _usage_output_tokens(response)
        self.ctx.tokens_generated += used if used is not None else streamed

"""真实模型端到端冒烟（gated）。

需要环境变量：
  AGENT_E2E_BASE_URL   OpenAI 兼容服务地址
  AGENT_E2E_API_KEY    API Key
  AGENT_E2E_MODEL      模型名（必须支持原生 Tool Calling）

未配置时整组跳过。覆盖任务书 §8 要求的真实模型场景：
  1. 多步工具调用 + 事件流 + AT-14 能力检测
  2. HITL 中断恢复（interrupt → Command(resume)）
  3. 大上下文卸载（FilesystemMiddleware 把大工具结果落盘）
"""
import asyncio
import os

import pytest
from langchain_core.messages import HumanMessage

pytestmark = pytest.mark.skipif(
    not (os.getenv("AGENT_E2E_BASE_URL") and os.getenv("AGENT_E2E_API_KEY") and os.getenv("AGENT_E2E_MODEL")),
    reason="未配置 AGENT_E2E_BASE_URL/AGENT_E2E_API_KEY/AGENT_E2E_MODEL，跳过真实模型 E2E",
)


@pytest.mark.asyncio
async def test_real_model_tool_calling_e2e():
    from langchain_core.tools import tool

    from app.services.agent_runtime.model_adapter import ModelCapabilityError, build_chat_model

    provider = {
        "name": "e2e",
        "base_url": os.environ["AGENT_E2E_BASE_URL"],
        "api_key": os.environ["AGENT_E2E_API_KEY"],
        "extra_config": {},
    }
    model_config = {
        "name": os.environ["AGENT_E2E_MODEL"],
        "supports_function_call": True,
        "supports_streaming": True,
        "extra_params": {"temperature": 0.0, "max_tokens": 512},
    }

    # AT-14：能力检测
    with pytest.raises(ModelCapabilityError):
        build_chat_model(provider, {**model_config, "supports_function_call": False})

    @tool
    def multiply(a: float, b: float) -> str:
        """计算两个数的乘积"""
        return str(a * b)

    # 真实模型 + 真实工具调用链路（create_agent 等价于 factory 的图组装路径）
    from langchain.agents import create_agent

    chat_model = build_chat_model(provider, model_config)
    agent = create_agent(chat_model, [multiply], system_prompt="你必须使用 multiply 工具。")

    events = []
    async for mode, chunk in agent.astream(
        {"messages": [HumanMessage(content="计算 17 * 23 的结果")]},
        stream_mode=["updates", "messages"],
    ):
        events.append((mode, chunk))

    final = None
    tool_used = False
    for mode, chunk in events:
        if mode == "updates" and isinstance(chunk, dict):
            for v in chunk.values():
                if isinstance(v, dict) and "messages" in v:
                    for m in v["messages"]:
                        if getattr(m, "tool_calls", None):
                            tool_used = True
                    final = v["messages"][-1].content
    assert tool_used, "真实模型应调用 multiply 工具"
    assert final and "391" in final, f"最终答案应包含 391: {final}"
    print("REAL MODEL E2E PASS:", final)


def _e2e_model_config():
    return {
        "name": os.environ["AGENT_E2E_MODEL"],
        "supports_function_call": True,
        "supports_streaming": True,
        "extra_params": {"temperature": 0.0, "max_tokens": 512},
    }


def _e2e_provider():
    return {
        "name": "e2e",
        "base_url": os.environ["AGENT_E2E_BASE_URL"],
        "api_key": os.environ["AGENT_E2E_API_KEY"],
        "extra_config": {},
    }


@pytest.mark.asyncio
async def test_real_model_hitl_interrupt_resume_e2e():
    """真实模型 + HITL：高危工具中断 → 批准 → 精确恢复执行（§8 真实模型场景 2）"""
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from app.services.agent_runtime.model_adapter import build_chat_model

    @tool
    def risky_deploy(service: str) -> str:
        """高危操作：部署指定服务（需人工审批）"""
        return f"deployed:{service}"

    chat_model = build_chat_model(_e2e_provider(), _e2e_model_config())
    agent = create_agent(
        chat_model, [risky_deploy],
        system_prompt="你必须使用 risky_deploy 工具完成部署请求。",
        checkpointer=InMemorySaver(),
        middleware=[HumanInTheLoopMiddleware(
            interrupt_on={"risky_deploy": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
        )],
    )
    config = {"configurable": {"thread_id": "e2e-hitl-1"}}
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="部署 billing 服务")]}, config=config
    )
    interrupts = result.get("__interrupt__") or ()
    assert interrupts, "真实模型的高危工具调用应触发 HITL 中断"
    assert interrupts[0].value.get("action_requests"), "中断应包含待审批动作"

    resumed = await agent.ainvoke(
        Command(resume={"decisions": [{"type": "approve", "message": "ok"}]}),
        config=config,
    )
    final = resumed["messages"][-1].content
    assert final, "恢复后应有最终回答"
    tool_executed = any(
        "deployed:billing" in str(getattr(m, "content", ""))
        for m in resumed["messages"]
    )
    assert tool_executed, f"批准后工具应精确恢复执行: {final}"
    print("REAL MODEL HITL E2E PASS:", final)


@pytest.mark.asyncio
async def test_real_model_large_context_unload_e2e():
    """真实模型 + 大上下文卸载：2MB 工具输出由 FilesystemMiddleware 落盘（§8 真实模型场景 3）"""
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    from deepagents.backends import StateBackend
    from deepagents.middleware import FilesystemMiddleware

    from app.services.agent_runtime.model_adapter import build_chat_model

    big_marker = "BIGDATA-" + "x" * (2 * 1024 * 1024)

    @tool
    def big_data() -> str:
        """返回一份超大数据（约 2MB）"""
        return big_marker

    chat_model = build_chat_model(_e2e_provider(), _e2e_model_config())
    agent = create_agent(
        chat_model, [big_data],
        system_prompt="先调用 big_data 工具，然后回答：数据是否以 BIGDATA 开头？",
        middleware=[FilesystemMiddleware(backend=StateBackend())],
    )
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="读取大数据并确认前缀")]},
        config={"configurable": {"thread_id": "e2e-ctx-1"}, "recursion_limit": 30},
    )
    tool_msgs = [str(getattr(m, "content", "")) for m in result["messages"] if getattr(m, "type", "") == "tool"]
    unloaded = any("too large" in c or "Tool result too large" in c for c in tool_msgs)
    assert unloaded, f"2MB 工具输出应被卸载到文件系统: {[c[:120] for c in tool_msgs]}"
    final = result["messages"][-1].content
    assert final, "卸载后模型仍应给出最终回答"
    print("REAL MODEL LARGE-CONTEXT E2E PASS:", final[:200])

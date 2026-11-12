"""DeepAgents Runtime 最小演示脚本（无 DB、无模型服务依赖）。

演示内容（对应任务书 §9「最小演示脚本」）：
1. FakeModel 多步原生 tool calling（datetime_now -> calculator -> 最终回答）
2. 全程打印旧 SSE 契约事件流（data: {json}，含 seq 序号）
3. HITL：calculator 触发审批中断 -> 批准 -> 精确恢复执行 -> 完成

使用真实的运行时组件组装（factory.build_agent / ToolAdapterFactory /
ToolPolicy / EventBus / LegacyEventAdapter），仅模型为脚本化 FakeChatModel。

运行：
    cd backend
    python scripts/demo_runtime.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.services.agent_runtime.context import (  # noqa: E402
    ExecutionBudget,
    RuntimeContext,
    RunScope,
)
from app.services.agent_runtime.events import (  # noqa: E402
    APPROVAL_REQUIRED,
    MODEL_DELTA,
    RUN_COMPLETED,
    RUN_WAITING_APPROVAL,
    EventBus,
    LegacyEventAdapter,
    RuntimeEvent,
    sse_format,
)
from app.services.agent_runtime.factory import build_agent  # noqa: E402
from app.services.agent_runtime.tools.adapter import ToolAdapterFactory  # noqa: E402
from app.services.agent_runtime.tools.policy import ToolPolicy  # noqa: E402


# ── 脚本化 FakeChatModel ────────────────────────────────────────────────────

class FakeChatModel(BaseChatModel):
    script: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake-demo"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        item = self.script.pop(0) if self.script else AIMessage(content="（脚本结束）")
        return ChatResult(generations=[ChatGeneration(message=item)])


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


# ── 运行驱动（与 service._consume_stream 同构，事件进 EventBus）────────────

async def drive(agent, payload, config, bus: EventBus, run_id: str) -> None:
    interrupted = False
    async for mode, chunk in agent.astream(payload, config=config, stream_mode=["updates", "messages"]):
        if mode == "messages":
            msg, meta = chunk
            if isinstance(msg, (AIMessage, AIMessageChunk)) and meta.get("langgraph_node") == "model":
                text = msg.content if isinstance(msg.content, str) else ""
                if text:
                    await bus.publish(RuntimeEvent(type=MODEL_DELTA, run_id=run_id, data={"text": text}))
        elif mode == "updates" and isinstance(chunk, dict):
            if "__interrupt__" in chunk:
                interrupted = True
                actions = []
                for it in chunk["__interrupt__"]:
                    value = getattr(it, "value", it)
                    for req in (value.get("action_requests") or []):
                        actions.append({
                            "tool": req.get("name") or req.get("action"),
                            "args": req.get("args"),
                        })
                await bus.publish(RuntimeEvent(type=APPROVAL_REQUIRED, run_id=run_id, data={"actions": actions}))
    if interrupted:
        await bus.publish(RuntimeEvent(type=RUN_WAITING_APPROVAL, run_id=run_id, data={"actions": []}))


async def consume(bus: EventBus, adapter: LegacyEventAdapter) -> None:
    """打印旧 SSE 契约事件流（data: {json}）。"""
    async for ev in bus.events():
        for legacy in adapter.adapt(ev):
            sys.stdout.write(sse_format(legacy))
            sys.stdout.flush()


# ── 主流程 ──────────────────────────────────────────────────────────────────

async def main() -> None:
    # 文件后端落到临时目录，演示不污染工作区
    from app.core.config import settings

    settings.AGENT_FILES_ROOT = os.path.join(tempfile.mkdtemp(), "agent_files")

    run_id = "demo-run-1"
    model = FakeChatModel(script=[
        tool_call("datetime_now", {}, "c1"),
        tool_call("calculator", {"expression": "21*2"}, "c2"),
        AIMessage(content="计算完成：21*2 = 42。"),
    ])

    ctx = RuntimeContext(
        scope=RunScope(user_id=1),
        run_id=run_id,
        thread_id="demo-thread",
        agent_id=0,
        agent_config={"id": 0, "system_prompt": "你是一个演示助手。", "temperature": "0.1"},
        budget=ExecutionBudget(max_steps=10, max_tool_calls=5, max_model_calls=10, run_timeout_seconds=30),
    )
    # 演示：calculator 强制走 HITL（生产中 hitl_tools 为高危工具集）
    policy = ToolPolicy(allowed_tools={"datetime_now", "calculator"}, hitl_tools={"calculator"})
    bus = EventBus()
    bus.bind_run(thread_id=ctx.thread_id, agent_id=ctx.agent_id, trace_id=run_id)
    adapter = LegacyEventAdapter(run_id)

    # 真实 factory 组装（仅模型为 Fake）
    import app.services.agent_runtime.factory as factory_mod
    from app.services.tools import get_tool_by_name

    factory_mod.build_chat_model = lambda *a, **k: model
    tool_specs = [get_tool_by_name("datetime_now"), get_tool_by_name("calculator")]
    checkpointer = InMemorySaver()  # 两阶段共用，thread 状态延续
    agent = build_agent(
        ctx, policy, bus,
        provider={"name": "fake", "base_url": "http://fake", "api_key": "none"},
        model_config={"name": "fake-demo", "supports_function_call": True},
        tool_specs=tool_specs,
        checkpointer=checkpointer,
    )

    config = {"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 50}

    print("════ 阶段 1：多步工具调用 + HITL 审批中断 ════")
    consumer = asyncio.create_task(consume(bus, adapter))
    await drive(
        agent,
        {"messages": [HumanMessage(content="先看时间，再算 21*2")]},
        config, bus, run_id,
    )
    await consumer
    print("── 模型/工具已在审批前暂停；模拟审批人批准 ──")

    print("════ 阶段 2：批准后精确恢复 ════")
    bus2 = EventBus()
    bus2.bind_run(thread_id=ctx.thread_id, agent_id=ctx.agent_id, trace_id=run_id)
    adapter2 = LegacyEventAdapter(run_id)  # 恢复阶段新适配器（与 service.resume_run 一致）
    consumer2 = asyncio.create_task(consume(bus2, adapter2))
    # 与 service.resume_run 一致：重建 agent（挂新 bus），复用同一 checkpointer，thread 状态延续
    agent = build_agent(
        ctx, policy, bus2,
        provider={"name": "fake", "base_url": "http://fake", "api_key": "none"},
        model_config={"name": "fake-demo", "supports_function_call": True},
        tool_specs=tool_specs,
        checkpointer=checkpointer,
    )
    await drive(
        agent,
        Command(resume={"decisions": [{"type": "approve", "message": "同意执行"}]}),
        config, bus2, run_id,
    )
    await bus2.publish(RuntimeEvent(
        type=RUN_COMPLETED, run_id=run_id, data={"final_text": "计算完成：21*2 = 42。"},
    ))
    await consumer2
    print("\n演示完成 ✅（FakeModel 多步 tool calling + HITL 中断恢复 + SSE 事件流）")


if __name__ == "__main__":
    asyncio.run(main())

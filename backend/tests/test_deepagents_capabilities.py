"""DeepAgents 能力深度测试：工具对 Agent 的实际支撑验证。

覆盖（全部实测断言，先真实跑通再固化）：
- A1 文件工具闭环（write_file → read_file → grep → edit_file → ls）：
  /workspace/ 路由真实文件系统（FilesystemBackend）+ 默认路由 StateBackend 虚拟文件
- A2 execute 沙箱边界：非 Sandbox 后端诚实拒绝（不执行、不伪装）
- A3 write_todos 规划：TodoListMiddleware 状态更新
- A4 task 子代理委派：收窄工具的子代理执行并把摘要带回父代理
- B  内部工具事件桥：DeepAgents 内部工具（不经 ToolAdapterFactory）的调用
  产生 TOOL 事件并计入预算；平台工具事件不重复

运行：
    pytest backend/tests/test_deepagents_capabilities.py -q
"""
import asyncio
import json
import os
import uuid
from typing import Any, Dict, List

import pytest
import pytest_asyncio
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from sqlalchemy import create_engine, delete, select

from app.core.database import AsyncSessionLocal, init_db
from app.models.agent import Agent, AgentRun, AgentRunEvent, AgentSession, AgentToolExecution
from app.models.model_provider import ModelConfig, ModelProvider
from app.models.user import User
from app.services.agent_runtime.events import TOOL_COMPLETED, TOOL_STARTED
from app.services.agent_runtime.lifecycle import STATUS_COMPLETED

db_url = os.environ.get("DATABASE_SYNC_URL", "sqlite:///./data/test.db")
sync_engine = create_engine(db_url)


class FakeChatModel(BaseChatModel):
    """脚本化模型（无网络）。"""

    script: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        out = self.script.pop(0) if self.script else AIMessage(content="EMPTY")
        return ChatResult(generations=[ChatGeneration(message=out)])


def _turn(name: str, args: dict, cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def _tool_msgs(result: dict) -> List[ToolMessage]:
    return [m for m in result["messages"] if isinstance(m, ToolMessage)]


def _build(script, *, subagents=None, middleware=None, backend=None):
    from deepagents import create_deep_agent

    from app.services.agent_runtime.backends import build_backend

    return create_deep_agent(
        FakeChatModel(script=list(script)),
        [],
        system_prompt="测试",
        middleware=middleware or [],
        subagents=subagents,
        backend=backend or build_backend(999, 999, 999),
    )


# ══════════════════════════ A：DeepAgents 工具能力闭环 ══════════════════════


@pytest.mark.asyncio
async def test_a1_file_tools_roundtrip_on_workspace(tmp_path, monkeypatch):
    """文件工具闭环：写→读→搜→改→读→列，读回内容与磁盘文件双一致。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path), raising=False)
    agent = _build([
        _turn("write_file", {"file_path": "/workspace/hello.txt", "content": "DEEPBOT-XYZ"}, "t1"),
        _turn("read_file", {"file_path": "/workspace/hello.txt"}, "t2"),
        _turn("grep", {"pattern": "DEEPBOT", "path": "/workspace/"}, "t3"),
        _turn("edit_file", {"file_path": "/workspace/hello.txt",
                            "old_string": "XYZ", "new_string": "ABC"}, "t4"),
        _turn("read_file", {"file_path": "/workspace/hello.txt"}, "t5"),
        _turn("ls", {"path": "/workspace/"}, "t6"),
        AIMessage(content="完成"),
    ])
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="做文件实验")]},
        config={"configurable": {"thread_id": f"da-{uuid.uuid4().hex[:8]}"}, "recursion_limit": 30},
    )
    by_id = {m.tool_call_id: m for m in _tool_msgs(result)}
    assert by_id["t2"].status == "success" and "DEEPBOT-XYZ" in str(by_id["t2"].content)
    assert by_id["t3"].status == "success" and "hello.txt" in str(by_id["t3"].content)
    assert by_id["t4"].status == "success"
    # edit 后读回新内容（写→改→读真闭环）
    assert "DEEPBOT-ABC" in str(by_id["t5"].content) and "DEEPBOT-XYZ" not in str(by_id["t5"].content)
    assert by_id["t6"].status == "success" and "hello.txt" in str(by_id["t6"].content)
    # 磁盘真实文件（/workspace/ 路由 FilesystemBackend）：前缀被剥离映射到会话根目录
    disk = tmp_path / "999" / "999" / "999" / "hello.txt"
    assert disk.exists() and disk.read_text(encoding="utf-8") == "DEEPBOT-ABC"


@pytest.mark.asyncio
async def test_a2_state_backend_virtual_files(tmp_path, monkeypatch):
    """默认路由 StateBackend：虚拟文件可写读回，但不落磁盘。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path), raising=False)
    agent = _build([
        _turn("write_file", {"file_path": "/memo.txt", "content": "S1-VIRT"}, "t1"),
        _turn("read_file", {"file_path": "/memo.txt"}, "t2"),
        AIMessage(content="完成"),
    ])
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="记个备忘")]},
        config={"configurable": {"thread_id": f"da-{uuid.uuid4().hex[:8]}"}, "recursion_limit": 30},
    )
    by_id = {m.tool_call_id: m for m in _tool_msgs(result)}
    assert by_id["t1"].status == "success"
    assert "S1-VIRT" in str(by_id["t2"].content)
    assert not (tmp_path / "999" / "999" / "999" / "memo.txt").exists()


@pytest.mark.asyncio
async def test_a3_execute_sandbox_boundary():
    """execute 边界：非 Sandbox 后端诚实拒绝（不执行命令、不伪装成功）。"""
    agent = _build([
        _turn("execute", {"command": "echo hi"}, "t1"),
        AIMessage(content="完成"),
    ])
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="跑命令")]},
        config={"configurable": {"thread_id": f"da-{uuid.uuid4().hex[:8]}"}, "recursion_limit": 30},
    )
    msg = {m.tool_call_id: m for m in _tool_msgs(result)}["t1"]
    assert msg.status == "error"
    content = str(msg.content)
    assert "SandboxBackendProtocol" in content or "not available" in content


@pytest.mark.asyncio
async def test_a4_write_todos_planning_state():
    """write_todos 规划：TodoListMiddleware 把计划写入图状态。"""
    from langchain.agents.middleware import TodoListMiddleware

    agent = _build([
        _turn("write_todos", {"todos": [{"content": "步骤A", "status": "in_progress"},
                                        {"content": "步骤B", "status": "pending"}]}, "t1"),
        AIMessage(content="计划已建"),
    ], middleware=[TodoListMiddleware()])
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="做个计划")]},
        config={"configurable": {"thread_id": f"da-{uuid.uuid4().hex[:8]}"}, "recursion_limit": 30},
    )
    todos = result.get("todos") or []
    assert [t.get("content") for t in todos] == ["步骤A", "步骤B"]
    msgs = {m.tool_call_id: m for m in _tool_msgs(result)}
    assert msgs["t1"].status == "success"


@tool
def calculator(expression: str) -> str:
    """计算数学表达式"""
    return "42"


@pytest.mark.asyncio
async def test_a5_subagent_task_delegation():
    """task 子代理委派：子代理用收窄工具执行并把摘要带回父代理。"""
    agent = _build([
        _turn("task", {"description": "计算 6*7", "subagent_type": "researcher"}, "t1"),
        _turn("calculator", {"expression": "6*7"}, "s1"),   # 子代理模型轮
        AIMessage(content="子代理结论：42"),                  # 子代理收尾
        AIMessage(content="最终完成"),                       # 父收尾
    ], subagents=[{
        "name": "researcher", "description": "研究检索子代理",
        "system_prompt": "你是研究检索子代理。", "tools": [calculator],
    }])
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="派发调研")]},
        config={"configurable": {"thread_id": f"da-{uuid.uuid4().hex[:8]}"}, "recursion_limit": 30},
    )
    msg = {m.tool_call_id: m for m in _tool_msgs(result)}["t1"]
    assert msg.status == "success"
    assert "子代理结论：42" in str(msg.content), f"task 应返回子代理摘要: {msg.content}"


# ══════════════════════════ B：内部工具事件桥（全链路 Service）═══════════════


@pytest.fixture()
def db_setup():
    from app.core.database import Base

    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(delete(AgentRunEvent))
        conn.execute(delete(AgentToolExecution))
        conn.execute(delete(AgentRun))
        conn.execute(delete(AgentSession))
        conn.execute(delete(Agent).where(Agent.name.in_(["da-cap-agent"])))
        conn.execute(delete(ModelConfig).where(ModelConfig.name.in_(["da-cap-model"])))
        conn.execute(delete(ModelProvider).where(ModelProvider.name.in_(["da-cap-provider"])))
        conn.execute(delete(User).where(User.username.in_(["da-cap-user"])))
        u = conn.execute(User.__table__.insert().values(
            username="da-cap-user", email="da-cap@test.local",
            hashed_password="x", is_active=True,
        ))
        user_id = u.inserted_primary_key[0]
        p = conn.execute(ModelProvider.__table__.insert().values(
            name="da-cap-provider", display_name="DA Cap", provider_type="openai_compatible",
            base_url="http://fake.local/v1", api_key="none", is_active=True,
        ))
        mc = conn.execute(ModelConfig.__table__.insert().values(
            provider_id=p.inserted_primary_key[0], name="da-cap-model", display_name="DA Cap",
            model_type="chat", is_active=True, supports_streaming=True,
            supports_function_call=True,
        ))
        a = conn.execute(Agent.__table__.insert().values(
            name="da-cap-agent", system_prompt="测试", max_iterations=5,
            temperature="0.3", tools=[], is_active=True, owner_id=user_id,
            model_config_id=mc.inserted_primary_key[0],
        ))
    return {"user_id": user_id, "agent_id": a.inserted_primary_key[0],
            "model_config_id": mc.inserted_primary_key[0]}


def _agent_config(setup, tools=None) -> Dict[str, Any]:
    return {
        "id": setup["agent_id"],
        "model_config_id": setup["model_config_id"],
        "system_prompt": "测试",
        "max_iterations": 5,
        "temperature": "0.3",
        "tools": tools or [],
    }


async def _run(setup, script, tools=None):
    import app.services.agent_runtime.factory as factory_mod
    from app.services.agent_runtime.service import AgentRuntimeService
    from app.services.agent_runtime.worker import RunWorker

    fake = FakeChatModel(script=list(script))
    factory_mod.build_chat_model = lambda *a, **k: fake  # monkeypatch 等效（函数级还原见测试）
    svc = AgentRuntimeService()
    svc.worker = RunWorker(svc, worker_id="w-da-cap", lease_seconds=30)

    async with AsyncSessionLocal() as s:
        sess = AgentSession(agent_id=setup["agent_id"], user_id=setup["user_id"], messages=[])
        s.add(sess)
        await s.flush()
        sid = sess.id
        await s.commit()

    user_message = {"role": "user", "content": "测试内部工具事件"}
    result = await svc.run_to_completion(
        agent_config=_agent_config(setup, tools),
        messages=[user_message],
        user_id=setup["user_id"],
        session_id=sid,
        idempotency_key=f"da-cap-{uuid.uuid4().hex}",
        user_message=user_message,
    )
    return result


@pytest.mark.asyncio
async def test_b1_internal_tool_events_bridged_and_counted(db_setup, monkeypatch):
    """内部工具（write_file）经事件桥产生 TOOL 事件并计入预算计数。"""
    import app.services.agent_runtime.factory as factory_mod

    real = factory_mod.build_chat_model
    try:
        result = await _run(db_setup, [
            _turn("write_file", {"file_path": "/workspace/bridge.txt", "content": "桥验证"}, "tc-1"),
            AIMessage(content="完成"),
        ])
    finally:
        factory_mod.build_chat_model = real

    assert result.get("status") in (STATUS_COMPLETED, "completed"), f"run 应完成: {result}"
    run_id = result["run_id"]
    async with AsyncSessionLocal() as s:
        evs = (await s.execute(
            select(AgentRunEvent.event_type, AgentRunEvent.payload)
            .where(AgentRunEvent.run_id == run_id)
        )).all()
        row = await s.get(AgentRun, run_id)

    started = [p for t, p in evs if t == TOOL_STARTED and "write_file" in str(p)]
    completed = [p for t, p in evs if t == TOOL_COMPLETED and "write_file" in str(p)]
    assert started, f"内部工具应有 tool_started 事件: {[(t, str(p)[:80]) for t, p in evs]}"
    assert completed, f"内部工具应有 tool_completed 事件: {[(t, str(p)[:80]) for t, p in evs]}"
    assert row.budget_tool_calls >= 1, "内部工具调用应计入预算计数"


@pytest.mark.asyncio
async def test_b2_platform_tool_events_not_duplicated(db_setup, monkeypatch):
    """平台工具事件由 adapter 发出，事件桥不得重复。"""
    import app.services.agent_runtime.factory as factory_mod

    real = factory_mod.build_chat_model
    try:
        result = await _run(db_setup, [
            _turn("calculator", {"expression": "1+1"}, "tc-1"),
            AIMessage(content="完成"),
        ], tools=[{"name": "calculator", "enabled": True}])
    finally:
        factory_mod.build_chat_model = real

    run_id = result["run_id"]
    async with AsyncSessionLocal() as s:
        evs = (await s.execute(
            select(AgentRunEvent.event_type, AgentRunEvent.payload)
            .where(AgentRunEvent.run_id == run_id)
        )).all()
    started = [p for t, p in evs if t == TOOL_STARTED and "calculator" in str(p)]
    assert len(started) == 1, f"calculator 的 tool_started 应恰 1 条（adapter 发出）: {len(started)}"

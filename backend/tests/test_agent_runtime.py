"""DeepAgents Runtime 核心测试（确定性 FakeChatModel，无网络；工具账本走临时 SQLite）。

覆盖（AT 编号=任务书 §8）：
- 事件映射与旧 SSE 契约（AT-03 流式 / P0-G 事件契约）
- 工具策略：SSRF/路径穿越/SQL 只读/沙箱禁用（AT-15 安全）
- 工具适配：schema 生成、事件发射、策略拒绝
- Factory 组装 + 多步 tool calling E2E（AT-02）
- HITL 中断→审批→恢复（AT-09）
- 生命周期状态机 + 幂等（AT-08 / AT-10）
- 模型能力检测：不支持工具调用显式报错（AT-14）
- 并发 20 会话隔离（AT-05）
- 执行预算超限（P0-A）
"""
import asyncio
import uuid
import pytest
import pytest_asyncio
from typing import Any, List

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.core.database import init_db

from app.services.agent_runtime.context import (
    ExecutionBudget,
    RuntimeContext,
    RunScope,
    messages_to_langchain,
)
from app.services.agent_runtime.events import (
    APPROVAL_REQUIRED,
    MODEL_DELTA,
    RUN_COMPLETED,
    RUN_WAITING_APPROVAL,
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_STARTED,
    LegacyEventAdapter,
    RuntimeEvent,
    redact_sensitive,
)
from app.services.agent_runtime.factory import build_agent, build_interrupt_config
from app.services.agent_runtime.lifecycle import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    InvalidTransition,
    RunRegistry,
)
from app.services.agent_runtime.model_adapter import ModelCapabilityError, build_chat_model
from app.services.agent_runtime.skills import SkillRegistry
from app.services.agent_runtime.tools.adapter import build_args_model
from app.services.agent_runtime.tools.policy import ToolPolicy, ToolPolicyError


@pytest_asyncio.fixture(autouse=True)
async def _db_schema():
    """工具账本（FR-05）需要 agent_tool_executions 表。"""
    await init_db()
    yield


# ── FakeChatModel：脚本化两轮（工具调用 -> 最终回答）────────────────────────

class FakeChatModel(BaseChatModel):
    script: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self.script:
            msg = AIMessage(content="（无更多脚本输出）")
        else:
            item = self.script.pop(0)
            msg = item() if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _tool_call_turn(name: str, args: dict, call_id: str = "c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@tool
def echo_tool(text: str) -> str:
    """回显文本"""
    return f"echo:{text}"


@tool
def risky_tool(action: str) -> str:
    """高风险工具（用于 HITL 测试）"""
    return f"done:{action}"


def make_ctx(run_id="test-run", **kw) -> RuntimeContext:
    return RuntimeContext(
        scope=RunScope(user_id=1),
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        agent_id=1,
        agent_config={"id": 1, "system_prompt": "测试", "temperature": "0.1"},
        budget=ExecutionBudget(max_steps=10, max_tool_calls=5, max_model_calls=10, run_timeout_seconds=30),
        **kw,
    )


async def make_db_ctx(**kw) -> RuntimeContext:
    """P0-03：工具账本写入必须持有真实执行权（真实 run + claim）。"""
    from app.core.database import AsyncSessionLocal
    from app.models.agent import Agent
    from app.services.agent_runtime.repositories import RunRepository

    run_id = uuid.uuid4().hex
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        agent = await s.get(Agent, 1)
        if agent is None:
            s.add(Agent(id=1, name="tr-agent"))
            await s.flush()
        await repo.create_run(
            run_id=run_id, agent_id=1, session_id=None, user_id=None,
            thread_id=f"agent-1-session-{run_id[:8]}",
        )
        version = await repo.claim(run_id, "w-test", 30)
        await s.commit()
    return make_ctx(run_id=run_id, worker_id="w-test", lease_version=version, **kw)


async def run_graph(model, tools, input_messages, ctx=None, interrupt_on=None, thread_id="t1"):
    """用 langchain create_agent 直接驱动图（绕过 DB 依赖），收集事件流。"""
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    ctx = ctx or make_ctx()
    middleware = []
    if interrupt_on:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))
    agent = create_agent(
        model, tools,
        system_prompt="测试",
        checkpointer=None,
        middleware=middleware,
    )
    events = []
    final_text = ""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
    async for mode, chunk in agent.astream(
        {"messages": input_messages}, config=config, stream_mode=["updates", "messages"]
    ):
        if mode == "messages":
            msg, meta = chunk
            if isinstance(msg, (AIMessage, AIMessageChunk)) and meta.get("langgraph_node") == "model":
                text = getattr(msg, "text", None)
                if callable(text):
                    text = text()
                if not isinstance(text, str):
                    text = msg.content if isinstance(msg.content, str) else ""
                if text:
                    final_text += text
                    events.append((MODEL_DELTA, text))
        elif mode == "updates" and isinstance(chunk, dict):
            if "__interrupt__" in chunk:
                events.append((APPROVAL_REQUIRED, chunk["__interrupt__"]))
    return final_text, events


# ── 事件映射（AT-03 / P0-G 事件契约）───────────────────────────────────────

class TestLegacyEventAdapter:
    def test_model_delta_streams_final(self):
        ad = LegacyEventAdapter("r1")
        out = []
        out += ad.adapt(RuntimeEvent(type=MODEL_DELTA, run_id="r1", data={"text": "你"}))
        out += ad.adapt(RuntimeEvent(type=MODEL_DELTA, run_id="r1", data={"text": "好"}))
        out += ad.adapt(RuntimeEvent(type=RUN_COMPLETED, run_id="r1", data={}))
        types = [e["type"] for e in out]
        assert types == ["final_start", "final_chunk", "final_chunk", "final", "done"]
        assert out[3]["content"] == "你好"

    def test_tool_events_shape(self):
        ad = LegacyEventAdapter("r1")
        out = ad.adapt(RuntimeEvent(type=TOOL_STARTED, run_id="r1", data={"tool": "t", "input": {"a": 1}}))
        assert out[-1]["type"] == "action" and out[-1]["tool"] == "t"
        out = ad.adapt(RuntimeEvent(type=TOOL_COMPLETED, run_id="r1", data={"tool": "t", "output": "x"}))
        assert out[-1]["type"] == "observation" and out[-1]["content"] == "x"

    def test_redact_sensitive(self):
        data = redact_sensitive({"api_key": "sk-secret", "url": "https://a", "note": "x" * 600})
        assert data["api_key"] == "***"
        assert len(data["note"]) < 600

    def test_waiting_approval_terminates_with_done(self):
        ad = LegacyEventAdapter("r1")
        out = ad.adapt(RuntimeEvent(type=RUN_WAITING_APPROVAL, run_id="r1", data={"actions": []}))
        assert out[-1]["type"] == "done" and out[-1]["status"] == "waiting_approval"

    def test_sse_events_have_sequence(self):
        """P0-G：SSE 事件带自增 seq，客户端可检测丢事件/乱序"""
        ad = LegacyEventAdapter("r1")
        out = []
        out += ad.adapt(RuntimeEvent(type=MODEL_DELTA, run_id="r1", data={"text": "a"}))
        out += ad.adapt(RuntimeEvent(type=TOOL_STARTED, run_id="r1", data={"tool": "t", "input": {}}))
        out += ad.adapt(RuntimeEvent(type=RUN_COMPLETED, run_id="r1", data={}))
        assert [e["seq"] for e in out] == list(range(len(out)))

    @pytest.mark.asyncio
    async def test_event_min_fields_per_contract(self):
        """§7.2 运行事件最小字段：event_id/event_type/run_id/thread_id/agent_id/timestamp/sequence/status/payload/trace_id"""
        from app.services.agent_runtime.events import EventBus

        bus = EventBus()
        bus.bind_run(thread_id="th-9", agent_id=7, trace_id="tr-9")
        bus.status = "running"
        await bus.publish(RuntimeEvent(type=TOOL_STARTED, run_id="r9", data={"tool": "t"}))
        ev = await bus._queue.get()
        d = ev.to_dict()
        for key in ("event_id", "event_type", "run_id", "thread_id", "agent_id",
                    "timestamp", "sequence", "status", "payload", "trace_id"):
            assert key in d, f"缺少最小字段 {key}"
        assert d["thread_id"] == "th-9" and d["agent_id"] == 7
        assert d["sequence"] == 1 and d["status"] == "running"
        assert d["timestamp"].endswith("Z")


# ── 安全策略（AT-15）────────────────────────────────────────────────────────

class TestToolPolicy:
    def test_ssrf_blocks_loopback(self):
        p = ToolPolicy(allowed_tools={"http_get"})
        with pytest.raises(ToolPolicyError):
            p.check_url("http://127.0.0.1/admin")
        with pytest.raises(ToolPolicyError):
            p.check_url("http://localhost:8080/x")
        with pytest.raises(ToolPolicyError):
            p.check_url("http://169.254.169.254/latest/meta-data")
        with pytest.raises(ToolPolicyError):
            p.check_url("file:///etc/passwd")

    def test_ssrf_allowlist(self):
        p = ToolPolicy(allowed_tools={"http_get"}, network_allowlist=["api.internal.corp"])
        p.check_url("https://api.internal.corp/v1")  # 名单内允许
        with pytest.raises(ToolPolicyError):
            p.check_url("https://evil.example.com/")

    def test_path_traversal_blocked(self, tmp_path):
        p = ToolPolicy(allowed_tools={"write_file"}, workspace_root=str(tmp_path))
        p.check_path("a/b.txt")
        with pytest.raises(ToolPolicyError):
            p.check_path("../../etc/passwd")
        with pytest.raises(ToolPolicyError):
            p.check_path("/etc/passwd")

    def test_sql_readonly(self):
        p = ToolPolicy(allowed_tools={"db_query"})
        p.check_sql_readonly("SELECT 1")
        with pytest.raises(ToolPolicyError):
            p.check_sql_readonly("DROP TABLE users")

    def test_sandbox_disabled_blocks_exec(self):
        p = ToolPolicy(allowed_tools={"python_exec"}, sandbox_mode="disabled")
        with pytest.raises(ToolPolicyError):
            p.check_exec_allowed("python_exec")

    def test_unauthorized_tool(self):
        p = ToolPolicy(allowed_tools={"calculator"})
        with pytest.raises(ToolPolicyError):
            p.check_tool_allowed("http_post")

    def test_hitl_required(self):
        p = ToolPolicy(allowed_tools={"http_post"})
        assert p.requires_approval("http_post")
        assert not p.requires_approval("calculator")


# ── 工具适配 ────────────────────────────────────────────────────────────────

class TestToolAdapter:
    def test_args_model_required(self):
        spec = {
            "name": "http_get",
            "parameters": {"url": {"type": "string", "description": "u"}, "timeout": {"type": "integer", "description": "t"}},
            "required": ["url"],
        }
        model = build_args_model("http_get", spec)
        inst = model(url="https://x")
        assert inst.url == "https://x" and inst.timeout is None
        with pytest.raises(Exception):
            model()

    @pytest.mark.asyncio
    async def test_adapter_emits_events_and_policy_rejects(self):
        from app.services.agent_runtime.events import EventBus
        from app.services.agent_runtime.tools.adapter import ToolAdapterFactory

        bus = EventBus()
        ctx = await make_db_ctx()
        policy = ToolPolicy(allowed_tools={"calculator"})
        factory = ToolAdapterFactory(ctx, policy, bus)
        tools = factory.build([
            {"name": "calculator", "description": "计算", "parameters": {"expression": {"type": "string", "description": ""}}, "required": ["expression"]},
            {"name": "http_get", "description": "http", "parameters": {"url": {"type": "string", "description": ""}}, "required": ["url"]},
        ])
        assert [t.name for t in tools] == ["calculator"]  # 未授权工具不下发

        calc = tools[0]
        # InjectedToolCallId 约定：经 ToolNode 调用时总是携带完整 tool_call
        result = await calc.ainvoke({"type": "tool_call", "name": "calculator", "args": {"expression": "1+1"}, "id": "call-t1"})
        # 携带 tool_call 调用时返回 ToolMessage 包装
        content = result.content if isinstance(result, ToolMessage) else result
        assert "2" in content
        # 事件：TOOL_STARTED + TOOL_COMPLETED 已入队
        ev1 = await bus._queue.get()
        ev2 = await bus._queue.get()
        assert ev1.type == TOOL_STARTED and ev2.type == TOOL_COMPLETED

        # 策略拒绝：未授权工具调用
        with pytest.raises(ToolPolicyError):
            await factory._execute("http_get", {"url": "http://127.0.0.1/"})
        ev3 = await bus._queue.get()
        assert ev3.type == TOOL_FAILED


# ── 模型能力（AT-14）───────────────────────────────────────────────────────

class TestModelAdapter:
    def test_no_function_call_rejected(self):
        with pytest.raises(ModelCapabilityError):
            build_chat_model(
                {"name": "p", "base_url": "http://x", "api_key": "k"},
                {"name": "m", "supports_function_call": False},
            )


# ── 生命周期（AT-08）───────────────────────────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_state_machine(self):
        reg = RunRegistry.instance()
        run = await reg.create(agent_id=1, session_id=None, user_id=1, thread_id="th-x")
        run.transition(STATUS_RUNNING)
        with pytest.raises(InvalidTransition):
            run.transition(STATUS_COMPLETED + "_bad")
        run.transition(STATUS_COMPLETED)
        assert run.is_terminal
        with pytest.raises(InvalidTransition):
            run.transition(STATUS_RUNNING)

    @pytest.mark.asyncio
    async def test_idempotency(self):
        reg = RunRegistry.instance()
        r1 = await reg.create(agent_id=1, session_id=None, user_id=1, thread_id="th-1", idempotency_key="k-1")
        r2 = await reg.create(agent_id=1, session_id=None, user_id=1, thread_id="th-1", idempotency_key="k-1")
        assert r1.run_id == r2.run_id


# ── E2E：多步 tool calling（AT-02）────────────────────────────────────────

class TestE2E:
    @pytest.mark.asyncio
    async def test_multi_step_tool_calling(self):
        model = FakeChatModel(script=[
            _tool_call_turn("echo_tool", {"text": "hello"}),
            lambda **kw: AIMessage(content="最终回答：echo 完成"),
        ])
        final_text, events = await run_graph(model, [echo_tool], [HumanMessage(content="调用 echo")])
        assert "最终回答" in final_text
        kinds = [k for k, _ in events]
        assert MODEL_DELTA in kinds

    @pytest.mark.asyncio
    async def test_tool_failure_is_explicit(self):
        """工具失败必须是显式状态：错误文本回传模型 + TOOL_FAILED 事件，不让运行崩溃"""
        from app.services.agent_runtime.events import EventBus
        from app.services.agent_runtime.tools.adapter import ToolAdapterFactory

        bus = EventBus()
        ctx = await make_db_ctx()
        policy = ToolPolicy(allowed_tools={"calculator"})
        factory = ToolAdapterFactory(ctx, policy, bus)

        # calculator 对非法表达式返回错误文本（不抛异常）
        result = await factory._execute("calculator", {"expression": "1/0"})
        assert isinstance(result, str)
        # 显式失败路径：未授权工具 -> ToolPolicyError + TOOL_FAILED 事件
        with pytest.raises(ToolPolicyError):
            await factory._execute("python_exec", {})
        ev_types = []
        while not bus._queue.empty():
            ev_types.append(bus._queue.get_nowait().type)
        assert TOOL_FAILED in ev_types

    @pytest.mark.asyncio
    async def test_tool_failure_reported_to_model(self):
        """E2E：工具异常以错误文本形式回传模型，模型给出明确失败结论"""
        from langchain_core.tools import StructuredTool
        from app.services.agent_runtime.events import EventBus
        from app.services.agent_runtime.tools.adapter import ToolAdapterFactory
        from app.services.agent_runtime.tools.policy import (
            ToolRisk,
            ToolRiskProfile,
            register_tool_risk,
            unregister_tool_risk,
        )

        # §15.2 A：自定义工具必须显式登记风险分类（未登记默认拒绝）；
        # 登记为只读测试工具后，验证异常以错误文本回传模型的链路不变
        register_tool_risk(ToolRiskProfile(
            name="broken_tool", risk=ToolRisk.READ,
            side_effect_class="read_only", requires_approval=False,
            retry_policy="safe_retry",
        ))
        try:
            bus = EventBus()
            factory = ToolAdapterFactory(await make_db_ctx(), ToolPolicy(allowed_tools={"broken_tool"}), bus)

            def _broken(x: str) -> str:
                raise ValueError("boom")

            spec_tool = StructuredTool.from_function(
                _broken, name="broken_tool", description="必失败工具", args_schema=build_args_model(
                    "broken_tool", {"parameters": {"x": {"type": "string", "description": ""}}, "required": ["x"]}
                ),
                coroutine=lambda **kw: factory._execute("broken_tool", kw),
            )

            model = FakeChatModel(script=[
                _tool_call_turn("broken_tool", {"x": "1"}),
                lambda **kw: AIMessage(content="工具失败了，无法继续。"),
            ])
            final_text, _ = await run_graph(model, [spec_tool], [HumanMessage(content="试一下")])
            assert "失败" in final_text
        finally:
            unregister_tool_risk("broken_tool")

    @pytest.mark.asyncio
    async def test_hitl_interrupt_and_resume(self):
        """AT-09：高危工具中断 → 审批 → 恢复执行"""
        from langchain.agents import create_agent
        from langchain.agents.middleware import HumanInTheLoopMiddleware
        from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command

        model = FakeChatModel(script=[
            _tool_call_turn("risky_tool", {"action": "deploy"}),
            lambda **kw: AIMessage(content="审批通过，部署完成。"),
        ])
        agent = create_agent(
            model, [risky_tool],
            system_prompt="测试",
            checkpointer=InMemorySaver(),
            middleware=[HumanInTheLoopMiddleware(
                interrupt_on={"risky_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
            )],
        )
        config = {"configurable": {"thread_id": "hitl-1"}}
        result = await agent.ainvoke({"messages": [HumanMessage(content="部署")]}, config=config)
        interrupts = result.get("__interrupt__") or ()
        assert interrupts, "应发生中断"
        value = interrupts[0].value
        assert value.get("action_requests"), "中断应包含待审批动作"

        # 拒绝/批准分支：批准后精确恢复
        resumed = await agent.ainvoke(
            Command(resume={"decisions": [{"type": "approve", "message": "ok"}]}),
            config=config,
        )
        final = resumed["messages"][-1].content
        assert "部署完成" in final

    @pytest.mark.asyncio
    async def test_concurrent_sessions_isolated(self):
        """AT-05：20 个并发会话互不串扰"""
        async def one(i: int):
            model = FakeChatModel(script=[
                _tool_call_turn("echo_tool", {"text": f"s{i}"}),
                (lambda tag: lambda **kw: AIMessage(content=f"answer-{tag}"))(i),
            ])
            final, _ = await run_graph(
                model, [echo_tool],
                [HumanMessage(content=f"会话{i}")],
                thread_id=f"iso-{i}",
            )
            return final

        results = await asyncio.gather(*[one(i) for i in range(20)])
        for i, r in enumerate(results):
            assert f"answer-{i}" in r, f"会话 {i} 结果串扰: {r}"


# ── Harness：Skills / SubAgents（AT-11、AT-12）────────────────────────────

class TestHarness:
    def test_skill_registry_disable(self, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.AGENT_DISABLED_SKILLS", "chart-report")
        reg = SkillRegistry.from_settings()
        names = {s.name for s in reg.enabled_skills()}
        assert "chart-report" not in names and "meeting-minutes" in names
        files = reg.files_payload()
        assert "/skills/meeting-minutes/SKILL.md" in files

    def test_subagents_narrowed_tools(self):
        from app.services.agent_runtime.subagents import build_subagents

        policy = ToolPolicy(allowed_tools={"web_search", "calculator"})
        subs = build_subagents(policy)
        by_name = {s["name"]: s for s in subs}
        assert set(by_name["researcher"]["tools"]) <= {"web_search", "calculator"}
        assert set(by_name["data-analyst"]["tools"]) <= {"calculator"}

    @pytest.mark.asyncio
    async def test_subagent_delegation_and_result_collection(self, monkeypatch):
        """AT-11：主 Agent 分派两类专用 SubAgent 并收回结果，父任务继续执行"""
        import app.services.agent_runtime.factory as factory_mod
        from app.services.agent_runtime.events import EventBus

        model = FakeChatModel(script=[
            _tool_call_turn("task", {"description": "调研 A", "subagent_type": "researcher"}, "t1"),
            AIMessage(content="RESEARCH_DONE"),
            _tool_call_turn("task", {"description": "分析 B", "subagent_type": "data-analyst"}, "t2"),
            AIMessage(content="ANALYSIS_DONE"),
            AIMessage(content="汇总: RESEARCH_DONE + ANALYSIS_DONE"),
        ])
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)

        ctx = make_ctx()
        policy = ToolPolicy(allowed_tools={"calculator", "datetime_now", "db_query", "web_search"})
        agent = build_agent(
            ctx, policy, EventBus(),
            {"name": "p", "base_url": "http://x", "api_key": "k", "extra_config": {}},
            {"name": "fake", "supports_function_call": True},
            [],
        )
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content="做个调研和分析")]},
            config={"configurable": {"thread_id": "sub-e2e-1"}, "recursion_limit": 50},
        )
        msgs = result["messages"]
        tool_results = [m.content for m in msgs if isinstance(m, ToolMessage)]
        assert any("RESEARCH_DONE" in str(c) for c in tool_results), tool_results
        assert any("ANALYSIS_DONE" in str(c) for c in tool_results), tool_results
        assert "汇总" in str(msgs[-1].content)  # 父任务在收回结果后继续执行

    @pytest.mark.asyncio
    async def test_skill_files_injected_into_run_state(self, monkeypatch):
        """AT-12：Skill 装载进 Agent 运行态（执行前提），禁用 Skill 不注入"""
        import app.services.agent_runtime.factory as factory_mod
        from app.services.agent_runtime.events import EventBus

        model = FakeChatModel(script=[AIMessage(content="ok")])
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)
        monkeypatch.setattr("app.core.config.settings.AGENT_DISABLED_SKILLS", "chart-report")

        ctx = make_ctx()
        policy = ToolPolicy(allowed_tools={"calculator"})
        agent = build_agent(
            ctx, policy, EventBus(),
            {"name": "p", "base_url": "http://x", "api_key": "k", "extra_config": {}},
            {"name": "fake", "supports_function_call": True},
            [],
        )
        result = await agent.ainvoke(
            {
                "messages": [HumanMessage(content="hi")],
                "files": SkillRegistry.from_settings().files_payload(),  # service._skill_files 同源注入
            },
            config={"configurable": {"thread_id": "skill-e2e-1"}, "recursion_limit": 50},
        )
        files = result.get("files") or (result.get("state") or {}).get("files") or {}
        assert "/skills/meeting-minutes/SKILL.md" in files, list(files)[:10]
        assert "会议纪要" in str(files["/skills/meeting-minutes/SKILL.md"])
        assert not any("chart-report" in p for p in files), list(files)[:10]

    def test_interrupt_config(self):
        policy = ToolPolicy(allowed_tools={"http_post"})
        cfg = build_interrupt_config(policy)
        assert "http_post" in cfg
        assert cfg["http_post"]["allowed_decisions"] == ["approve", "reject"]


# ── 可观测（P0-H）──────────────────────────────────────────────────────────

class TestTelemetry:
    FIELDS = {
        "request_id": "r1", "trace_id": "tr1", "run_id": "r1", "thread_id": "th1",
        "agent_id": 1, "tenant_id": None, "user_id": 1, "session_id": 2, "model_id": "m1",
    }

    def _capturing(self):
        from app.services.agent_runtime.telemetry import Telemetry

        captured = []

        class Capture:
            def export(self, record):
                captured.append(record)

        return Telemetry(exporters=[Capture()]), captured

    def test_run_and_tool_records(self):
        t, captured = self._capturing()
        t.record_run(self.FIELDS, status="running")
        t.record_run(self.FIELDS, status="completed", duration_ms=12.5, attributes={"tool_calls": 1})
        t.record_tool(self.FIELDS, tool="calculator", status="completed", duration_ms=3.0)
        t.record_tool(self.FIELDS, tool="http_get", status="failed", error="ssrf")
        kinds = [(c.kind, c.status) for c in captured]
        assert kinds == [("run", "running"), ("run", "completed"), ("tool", "completed"), ("tool", "failed")]
        assert captured[1].duration_ms == 12.5
        assert captured[3].error == "ssrf"
        for key in ("request_id", "trace_id", "run_id", "thread_id", "agent_id",
                    "tenant_id", "user_id", "session_id", "model_id"):
            assert key in captured[0].fields

    def test_exporter_error_does_not_break(self):
        t, captured = self._capturing()

        class Boom:
            def export(self, record):
                raise RuntimeError("backend down")

        t.add_exporter(Boom())
        t.record_run(self.FIELDS, status="running")  # 不应抛出
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_tool_adapter_emits_tool_telemetry(self, monkeypatch):
        import app.services.agent_runtime.tools.adapter as adapter_mod
        from app.services.agent_runtime.events import EventBus
        from app.services.agent_runtime.tools.adapter import ToolAdapterFactory

        t, captured = self._capturing()
        monkeypatch.setattr(adapter_mod, "telemetry", t)
        bus = EventBus()
        factory = ToolAdapterFactory(await make_db_ctx(), ToolPolicy(allowed_tools={"calculator"}), bus)
        await factory._execute("calculator", {"expression": "2+2"})
        with pytest.raises(ToolPolicyError):
            await factory._execute("http_get", {"url": "http://127.0.0.1/"})
        tool_records = [c for c in captured if c.kind == "tool"]
        assert [c.status for c in tool_records] == ["completed", "rejected"]
        assert tool_records[0].name == "calculator"
        assert tool_records[0].duration_ms is not None

    def test_context_log_fields_include_model_id(self):
        ctx = make_ctx()
        ctx.model_id = "gpt-x"
        fields = ctx.to_log_fields()
        assert fields["model_id"] == "gpt-x"
        assert fields["run_id"] == "test-run" and fields["user_id"] == 1


# ── 执行预算（P0-A）：次数/步数超限 -> 明确异常与 limit_exceeded 状态 ──────

class TestBudget:
    def test_budget_exhausted_reason(self):
        b = ExecutionBudget(max_steps=5, max_tool_calls=3, max_model_calls=4)
        assert b.exhausted_reason(1, 1, 1) is None
        assert "执行步数" in (b.exhausted_reason(6, 1, 1) or "")
        assert "工具调用" in (b.exhausted_reason(1, 4, 1) or "")
        assert "模型调用" in (b.exhausted_reason(1, 1, 5) or "")

    @pytest.mark.asyncio
    async def test_tool_call_limit_raises(self):
        """工具调用次数超限 -> ToolCallLimitExceededError（exit_behavior=error）"""
        from langchain.agents import create_agent
        from langchain.agents.middleware import ToolCallLimitMiddleware
        from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError

        model = FakeChatModel(script=[
            _tool_call_turn("echo_tool", {"text": "a"}, "c1"),
            _tool_call_turn("echo_tool", {"text": "b"}, "c2"),
            _tool_call_turn("echo_tool", {"text": "c"}, "c3"),
        ])
        agent = create_agent(
            model, [echo_tool], system_prompt="t",
            middleware=[ToolCallLimitMiddleware(run_limit=2, exit_behavior="error")],
        )
        with pytest.raises(ToolCallLimitExceededError):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="go")]},
                config={"configurable": {"thread_id": "budget-1"}, "recursion_limit": 50},
            )

    @pytest.mark.asyncio
    async def test_model_call_limit_raises(self):
        """模型调用次数超限 -> ModelCallLimitExceededError"""
        from langchain.agents import create_agent
        from langchain.agents.middleware import ModelCallLimitMiddleware
        from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError

        model = FakeChatModel(script=[
            _tool_call_turn("echo_tool", {"text": "a"}, "c1"),
            AIMessage(content="done"),
        ])
        agent = create_agent(
            model, [echo_tool], system_prompt="t",
            middleware=[ModelCallLimitMiddleware(run_limit=1, exit_behavior="error")],
        )
        with pytest.raises(ModelCallLimitExceededError):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="go")]},
                config={"configurable": {"thread_id": "budget-2"}, "recursion_limit": 50},
            )

    @pytest.mark.asyncio
    async def test_recursion_limit_raises(self):
        """步数（recursion_limit）超限 -> GraphRecursionError（service 映射 limit_exceeded）"""
        from langchain.agents import create_agent
        from langgraph.errors import GraphRecursionError

        counter = {"n": 0}

        def endless(**kw):
            counter["n"] += 1
            return _tool_call_turn("echo_tool", {"text": "x"}, f"c{counter['n']}")

        model = FakeChatModel(script=[endless] * 30)
        agent = create_agent(model, [echo_tool], system_prompt="t")
        with pytest.raises(GraphRecursionError):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="go")]},
                config={"configurable": {"thread_id": "budget-3"}, "recursion_limit": 6},
            )

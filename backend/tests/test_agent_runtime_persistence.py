"""DeepAgents Runtime 持久化/副作用/资源测试（确定性 FakeChatModel，无网络）。

覆盖（任务书 §8 验收场景补强证据）：
- AT-06 Checkpoint：进程重启（新连接/新实例）后同 thread 可恢复、多轮连续
- AT-07 多实例：两个独立 checkpointer 连同一 sqlite 文件互相可见状态
- AT-10 幂等副作用：HITL 批准后工具恰好执行一次，重复 resume 不重放
- AT-13 大上下文：大文件落 workspace 后端；大工具输出自动卸载，不整份进上下文
- AT-17 资源与回滚：取消运行后无残留 asyncio 任务；sqlite 连接可正常关闭
"""
import asyncio
from typing import Any, List

import aiosqlite
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command


class FakeChatModel(BaseChatModel):
    """脚本化模型：按序吐出脚本轮次，并记录每轮输入消息（验证历史可见性）。"""

    script: List[Any] = []
    seen: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append([getattr(m, "content", "") for m in messages])
        if not self.script:
            msg = AIMessage(content="（无更多脚本输出）")
        else:
            item = self.script.pop(0)
            msg = item() if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _tool_call_turn(name: str, args: dict, call_id: str = "c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


# ── AT-06：进程重启后恢复 + 多轮连续 ─────────────────────────────────────────

class TestCheckpointRestart:
    @pytest.mark.asyncio
    async def test_restart_recovery_and_continuity(self, tmp_path):
        db_path = str(tmp_path / "ckpt.db")
        cfg = {"configurable": {"thread_id": "sess-42"}}

        # 进程 1：写入第一轮
        conn1 = await aiosqlite.connect(db_path)
        saver1 = AsyncSqliteSaver(conn1)
        await saver1.setup()
        model1 = FakeChatModel(script=[AIMessage(content="第一轮回答")])
        agent1 = create_agent(model1, [], system_prompt="t", checkpointer=saver1)
        await agent1.ainvoke({"messages": [HumanMessage(content="我叫小明")]}, config=cfg)
        await conn1.close()

        # 进程 2：全新连接 + 全新 agent 实例（等价进程重启）
        conn2 = await aiosqlite.connect(db_path)
        saver2 = AsyncSqliteSaver(conn2)
        await saver2.setup()
        model2 = FakeChatModel(script=[AIMessage(content="第二轮回答")])
        agent2 = create_agent(model2, [], system_prompt="t", checkpointer=saver2)

        state = await agent2.aget_state(cfg)
        contents = [m.content for m in state.values["messages"]]
        assert "我叫小明" in contents, f"重启后历史丢失: {contents}"
        assert "第一轮回答" in contents

        # 同 thread 续跑：旧历史对模型可见（多轮连续）
        await agent2.ainvoke({"messages": [HumanMessage(content="我叫什么")]}, config=cfg)
        assert any("我叫小明" in str(c) for turn in model2.seen for c in turn), \
            f"续跑输入未包含历史: {model2.seen}"

        state = await agent2.aget_state(cfg)
        contents = [m.content for m in state.values["messages"]]
        assert "第二轮回答" in contents and contents.count("我叫小明") == 1
        await conn2.close()


# ── AT-07：多实例共享持久化后端 ─────────────────────────────────────────────

class TestMultiInstance:
    @pytest.mark.asyncio
    async def test_two_instances_share_thread_state(self, tmp_path):
        db_path = str(tmp_path / "shared.db")
        cfg = {"configurable": {"thread_id": "sess-m"}}

        conn_a = await aiosqlite.connect(db_path)
        saver_a = AsyncSqliteSaver(conn_a)
        await saver_a.setup()
        conn_b = await aiosqlite.connect(db_path)
        saver_b = AsyncSqliteSaver(conn_b)
        await saver_b.setup()

        # 实例 A 写入
        agent_a = create_agent(
            FakeChatModel(script=[AIMessage(content="A 的回答")]),
            [], system_prompt="t", checkpointer=saver_a,
        )
        await agent_a.ainvoke({"messages": [HumanMessage(content="来自 A")]}, config=cfg)

        # 实例 B 查询同一 thread 状态并续跑
        agent_b = create_agent(
            FakeChatModel(script=[AIMessage(content="B 的回答")]),
            [], system_prompt="t", checkpointer=saver_b,
        )
        state_b = await agent_b.aget_state(cfg)
        contents = [m.content for m in state_b.values["messages"]]
        assert "来自 A" in contents and "A 的回答" in contents, f"实例 B 不可见 A 的状态: {contents}"

        await agent_b.ainvoke({"messages": [HumanMessage(content="来自 B")]}, config=cfg)

        # 实例 A 反向可见 B 的写入
        state_a = await agent_a.aget_state(cfg)
        contents = [m.content for m in state_a.values["messages"]]
        assert "B 的回答" in contents, f"实例 A 不可见 B 的状态: {contents}"

        await conn_a.close()
        await conn_b.close()

    @pytest.mark.asyncio
    async def test_checkpointer_factory_sqlite_fresh_db(self, tmp_path, monkeypatch):
        """CheckpointerFactory sqlite 分支在全新 DB 上可直接使用（setup 建表）。"""
        monkeypatch.setattr(
            "app.core.config.settings.AGENT_CHECKPOINT_DSN",
            f"sqlite:///{tmp_path / 'factory.db'}",
        )
        from app.services.agent_runtime.persistence import CheckpointerFactory

        factory = CheckpointerFactory.get()
        saver = await factory.get_checkpointer()
        try:
            cfg = {"configurable": {"thread_id": "factory-1"}}
            model = FakeChatModel(script=[AIMessage(content="ok")])
            agent = create_agent(model, [], system_prompt="t", checkpointer=saver)
            result = await agent.ainvoke({"messages": [HumanMessage(content="hi")]}, config=cfg)
            assert result["messages"][-1].content == "ok"
        finally:
            # 关闭底层连接并重置工厂缓存，避免后台线程滞留/复用已关连接
            await saver.conn.close()
            factory._checkpointer = None
            factory._dsn = None


# ── AT-10：HITL 幂等副作用 ──────────────────────────────────────────────────

class TestIdempotentSideEffect:
    @pytest.mark.asyncio
    async def test_tool_executes_exactly_once_across_resumes(self):
        calls: List[str] = []

        @tool
        def risky_deploy(action: str) -> str:
            """高风险部署"""
            calls.append(action)
            return f"deployed:{action}"

        model = FakeChatModel(script=[
            _tool_call_turn("risky_deploy", {"action": "prod"}),
            lambda **kw: AIMessage(content="部署完成"),
            lambda **kw: AIMessage(content="不应再次触发工具"),
        ])
        agent = create_agent(
            model, [risky_deploy],
            system_prompt="t",
            checkpointer=InMemorySaver(),
            middleware=[HumanInTheLoopMiddleware(
                interrupt_on={"risky_deploy": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
            )],
        )
        cfg = {"configurable": {"thread_id": "idem-1"}}

        result = await agent.ainvoke({"messages": [HumanMessage(content="部署到生产")]}, config=cfg)
        assert result.get("__interrupt__"), "高危工具应触发审批中断"
        assert calls == [], "审批前工具不得执行"

        # 批准：恰好执行一次
        await agent.ainvoke(
            Command(resume={"decisions": [{"type": "approve", "message": "ok"}]}),
            config=cfg,
        )
        assert calls == ["prod"], f"批准后应恰好执行一次，实际: {calls}"

        # 重复 resume：不得重放外部副作用
        for _ in range(2):
            try:
                await agent.ainvoke(
                    Command(resume={"decisions": [{"type": "approve", "message": "重复审批"}]}),
                    config=cfg,
                )
            except Exception:
                pass  # 重复 resume 报错是允许行为；关键是副作用不重放
        assert calls == ["prod"], f"重复 resume 重放了副作用: {calls}"

    @pytest.mark.asyncio
    async def test_reject_prevents_execution(self):
        calls: List[str] = []

        @tool
        def risky_notify(msg: str) -> str:
            """高风险通知"""
            calls.append(msg)
            return "sent"

        model = FakeChatModel(script=[
            _tool_call_turn("risky_notify", {"msg": "x"}),
            lambda **kw: AIMessage(content="操作被拒绝，未执行。"),
        ])
        agent = create_agent(
            model, [risky_notify],
            system_prompt="t",
            checkpointer=InMemorySaver(),
            middleware=[HumanInTheLoopMiddleware(
                interrupt_on={"risky_notify": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
            )],
        )
        cfg = {"configurable": {"thread_id": "idem-2"}}
        result = await agent.ainvoke({"messages": [HumanMessage(content="发通知")]}, config=cfg)
        assert result.get("__interrupt__")
        await agent.ainvoke(
            Command(resume={"decisions": [{"type": "reject", "message": "不同意"}]}),
            config=cfg,
        )
        assert calls == [], "拒绝后工具不得执行"


# ── AT-13：大上下文/大文件/工具输出卸载 ─────────────────────────────────────

class TestLargeContext:
    BIG = "L" * 2_000_000  # 2MB 文本

    @pytest.mark.asyncio
    async def test_large_file_lands_in_workspace_backend(self, tmp_path, monkeypatch):
        """大文件写入 /workspace 后端：按用户/Agent/会话隔离落盘，可完整读回。"""
        monkeypatch.setattr("app.core.config.settings.AGENT_FILES_ROOT", str(tmp_path))
        from app.services.agent_runtime.backends import build_backend

        backend = build_backend(user_id=1, agent_id=2, session_id=3)
        await backend.awrite("/workspace/big.txt", self.BIG)
        result = await backend.aread("/workspace/big.txt")
        content = result.file_data["content"] if result.file_data else ""
        assert content == self.BIG, "大文件应可完整读回"

        # 物理落点在会话隔离目录内，不指向宿主机根目录
        host_path = tmp_path / "1" / "2" / "3" / "big.txt"
        assert host_path.exists(), f"文件应落在会话隔离目录: {host_path}"
        assert str(tmp_path) in str(host_path)

    @pytest.mark.asyncio
    async def test_big_tool_output_offloaded_not_inlined(self):
        """大工具输出自动卸载到虚拟文件系统：模型上下文里只留路径+预览。"""
        from deepagents import create_deep_agent

        big = self.BIG

        @tool
        def big_dump() -> str:
            """导出大结果"""
            return big

        model = FakeChatModel(script=[
            _tool_call_turn("big_dump", {}),
            lambda **kw: AIMessage(content="已导出大结果。"),
        ])
        agent = create_deep_agent(model, [big_dump], system_prompt="t")
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content="导出")]},
            config={"configurable": {"thread_id": "big-1"}},
        )
        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert tool_msgs, "应产生工具消息"
        tail = str(tool_msgs[-1].content)
        assert "Tool result too large" in tail, f"大输出应被卸载: {tail[:200]}"
        assert len(tail) < 100_000, f"卸载后上下文仍过大: {len(tail)}"
        assert big not in tail, "整份大结果不得内联进上下文"


# ── AT-17：取消后无资源泄漏 ─────────────────────────────────────────────────

class TestResourceLeak:
    @pytest.mark.asyncio
    async def test_cancel_leaves_no_tasks_and_closes_sqlite(self, tmp_path):
        started = asyncio.Event()
        blocker = asyncio.Event()

        @tool
        async def hang() -> str:
            """永不返回的工具"""
            started.set()
            await blocker.wait()
            return "never"

        db_path = str(tmp_path / "leak.db")
        conn = await aiosqlite.connect(db_path)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()

        model = FakeChatModel(script=[_tool_call_turn("hang", {}), AIMessage(content="x")])
        agent = create_agent(model, [hang], system_prompt="t", checkpointer=saver)
        cfg = {"configurable": {"thread_id": "leak-1"}}

        tasks_before = set(asyncio.all_tasks())
        task = asyncio.create_task(
            agent.ainvoke({"messages": [HumanMessage(content="go")]}, config=cfg)
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.1)  # 给事件循环清理时间
        leftover = {t for t in asyncio.all_tasks() if not t.done()} - tasks_before
        assert not leftover, f"取消后残留任务: {leftover}"

        # sqlite 连接可正常关闭（无未释放连接）
        await conn.close()
        with pytest.raises(Exception):
            await conn.execute("SELECT 1")  # 连接已关闭，不可再操作

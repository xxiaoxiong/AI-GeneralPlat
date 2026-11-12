"""P1-01 / P1-02 验收测试：API 幂等（§9.3）+ Session 并发锁 / 消息一致性 / Outbox 写回（§10.3）。

运行：
    pytest backend/tests/test_api_idempotency_session.py -q
"""
import asyncio
import json
import os
import uuid
from typing import Any, Dict, List

import pytest
import pytest_asyncio
import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import create_engine, delete, select

from app.core.database import AsyncSessionLocal, engine, init_db
from app.core.security import create_access_token
from app.main import app
from app.models.agent import (
    Agent,
    AgentApproval,
    AgentRun,
    AgentRunEvent,
    AgentSession,
    AgentToolExecution,
    SessionResultOutbox,
)
from app.models.user import User
from app.services.agent_runtime.events import RUN_COMPLETED, RUN_STARTED, RuntimeEvent
from app.services.agent_runtime.lifecycle import STATUS_COMPLETED
from app.services.agent_runtime.repositories import (
    RunRepository,
    SessionBusy,
)
from app.services.agent_runtime.repositories.session_repository import (
    SessionOutboxRepository,
    SessionRepository,
)
from app.services.agent_runtime.service import AgentRuntimeService
from app.services.agent_runtime.worker import RunWorker

db_url = os.environ.get("DATABASE_SYNC_URL", "sqlite:///./data/test.db")
sync_engine = create_engine(db_url)


class FakeChatModel(BaseChatModel):
    """脚本化模型（无网络、真实 Run/Session 流程）。"""

    script: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        out = self.script.pop(0) if self.script else AIMessage(content="EMPTY")
        return ChatResult(generations=[ChatGeneration(message=out)])


def _patch_model(monkeypatch, script):
    """替换工厂返回的模型为脚本 FakeChatModel（真实执行、真实 Run/Session 流程）。"""
    fake = FakeChatModel(script=list(script))
    import app.services.agent_runtime.factory as factory_mod

    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: fake)
    return fake


def _auth(uid: int) -> Dict[str, str]:
    # create_access_token(subject) 内部已封装 {"sub": str(subject)}
    token = create_access_token(uid)
    return {"Authorization": f"Bearer {token}"}


async def _iter_sse(resp) -> List[Dict]:
    events = []
    async for line in resp.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _make_service(*, worker_id="w-sess", lease_seconds=30):
    svc = AgentRuntimeService()
    svc.worker = RunWorker(svc, worker_id=worker_id, lease_seconds=lease_seconds)
    return svc


def _gate_drive(record: List[Dict], gate: asyncio.Event):
    """替换 _drive：进入即记录并阻塞在 gate 上（模拟执行中持有执行权），
    gate 放行后走真实完成提交序列（终态 + 同事务 Outbox 入队）。"""

    async def fake_drive(
        self, run, adapter, *, messages, resume,
        agent_config=None, use_memory_context="", db_info=None, user_id=None,
    ):
        record.append({"run_id": run.run_id})
        await run.bus.publish(RuntimeEvent(type=RUN_STARTED, run_id=run.run_id))
        await gate.wait()
        run.final_text = "ok"
        run.summary = {"tool_calls": 0, "model_calls": 1}
        run.transition(STATUS_COMPLETED)
        await self._commit(
            run, status=STATUS_COMPLETED, final_text="ok",
            summary=run.summary, finished=True,
        )
        await run.bus.publish(
            RuntimeEvent(type=RUN_COMPLETED, run_id=run.run_id, data={"final_text": "ok"})
        )

    return fake_drive


async def _wait_status(run_id, statuses, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with AsyncSessionLocal() as s:
            row = await RunRepository(s).get_run(run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.02)
    return None


async def _count(model, **filters) -> int:
    async with AsyncSessionLocal() as s:
        stmt = select(model)
        for k, v in filters.items():
            stmt = stmt.where(getattr(model, k) == v)
        res = await s.execute(stmt)
        return len(res.scalars().all())


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _db():
    await init_db()
    # 测试间清空持久化表（含 P1-02 outbox / session），保证计数与扫描断言干净
    async with AsyncSessionLocal() as s:
        for model in (
            SessionResultOutbox, AgentApproval, AgentRunEvent,
            AgentToolExecution, AgentRun, AgentSession,
        ):
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


@pytest.fixture(scope="module")
def db_setup():
    from app.core.database import Base
    from app.models.model_provider import ModelConfig, ModelProvider

    # 同步建表（幂等）：不依赖 fixture 实例化顺序
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(delete(AgentRunEvent))
        conn.execute(delete(AgentToolExecution))
        conn.execute(delete(AgentApproval))
        conn.execute(delete(SessionResultOutbox))
        conn.execute(delete(AgentRun))
        conn.execute(delete(AgentSession))
        conn.execute(delete(Agent).where(Agent.name.in_(["b5-agent", "b5-agent-2"])))
        conn.execute(delete(ModelConfig).where(ModelConfig.name.in_(["b5-model"])))
        conn.execute(delete(ModelProvider).where(ModelProvider.name.in_(["b5-provider"])))
        conn.execute(delete(User).where(User.username.in_(["b5-alice"])))
        r1 = conn.execute(User.__table__.insert().values(
            username="b5-alice", email="b5-alice@test.local",
            hashed_password="x", is_active=True,
        ))
        user_id = r1.inserted_primary_key[0]
        r2 = conn.execute(ModelProvider.__table__.insert().values(
            name="b5-provider", display_name="B5 Fake", provider_type="openai_compatible",
            base_url="http://fake.local/v1", api_key="none", is_active=True,
        ))
        r3 = conn.execute(ModelConfig.__table__.insert().values(
            provider_id=r2.inserted_primary_key[0], name="b5-model", display_name="B5 Fake",
            model_type="chat", is_active=True, supports_streaming=True,
            supports_function_call=True,
        ))
        r4 = conn.execute(Agent.__table__.insert().values(
            name="b5-agent", system_prompt="测试 Agent", max_iterations=3,
            temperature="0.7", tools=[], is_active=True, owner_id=user_id,
            model_config_id=r3.inserted_primary_key[0],
        ))
    return {
        "user1_id": user_id,
        "agent_id": r4.inserted_primary_key[0],
        "model_config_id": r3.inserted_primary_key[0],
    }


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    """httpx ASGI 客户端：app 与测试 helper 运行在同一个 event loop。

    （TestClient 的 portal 线程自建独立 loop，与 async helper 共享连接池时
    aiomysql 连接会跨 loop 报 Future attached to a different loop。）
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


def _chat_payload(message: str, session_id=None, stream=False) -> Dict[str, Any]:
    return {
        "message": message,
        "stream": stream,
        "use_memory": False,
        **({"session_id": session_id} if session_id else {}),
    }


# ══════════════════════════ §9.3：P1-01 API 幂等验收 ══════════════════════════


class TestApiIdempotency:
    """§9.3 验收：幂等键收敛 Service/Repository + 请求摘要校验。"""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_id01_same_key_same_request_10x_one_run(self, client, db_setup, monkeypatch):
        # §9.3-1：相同 Key 相同请求重试 10 次，只存在一个 Run
        _patch_model(monkeypatch, [AIMessage(content="IDEM_OK")])
        key = f"idem-{uuid.uuid4().hex}"
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": key}
        body = _chat_payload("幂等测试")

        run_ids = set()
        for _ in range(10):
            r = await client.post(
                f"/api/v1/agents/{db_setup['agent_id']}/chat", json=body, headers=headers
            )
            assert r.status_code == 200, r.text
            if r.json().get("run_id"):
                run_ids.add(r.json()["run_id"])

        assert len(run_ids) == 1, f"幂等命中应复用同一 Run: {run_ids}"
        with sync_engine.begin() as conn:
            rows = conn.execute(
                select(AgentRun.id).where(AgentRun.idempotency_key == key)
            ).all()
        assert len(rows) == 1, f"应只有一个 Run，实际 {len(rows)}"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_id02_same_key_different_message_409(self, client, db_setup, monkeypatch):
        # §9.3-2：相同 Key、不同 message → 409
        _patch_model(monkeypatch, [AIMessage(content="A_OK"), AIMessage(content="B_OK")])
        key = f"idem-{uuid.uuid4().hex}"
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": key}

        r1 = await client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/chat",
            json=_chat_payload("请求 A"), headers=headers,
        )
        assert r1.status_code == 200
        r2 = await client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/chat",
            json=_chat_payload("请求 B"), headers=headers,
        )
        assert r2.status_code == 409, f"不同请求摘要必须 409，实际 {r2.status_code}"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_id03_same_key_different_agent_409(self, client, db_setup, monkeypatch):
        # §9.3-3：相同 Key、不同 agent_id → 冲突
        _patch_model(monkeypatch, [AIMessage(content="A_OK"), AIMessage(content="B_OK")])
        r = await client.post(
            "/api/v1/agents",
            json={"name": "b5-agent-2", "system_prompt": "另一个 Agent", "tools": []},
            headers=_auth(db_setup["user1_id"]),
        )
        assert r.status_code == 200
        agent2_id = r.json()["id"]

        key = f"idem-{uuid.uuid4().hex}"
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": key}
        r1 = await client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/chat",
            json=_chat_payload("同 Key 跨 Agent"), headers=headers,
        )
        assert r1.status_code == 200
        r2 = await client.post(
            f"/api/v1/agents/{agent2_id}/chat",
            json=_chat_payload("同 Key 跨 Agent"), headers=headers,
        )
        assert r2.status_code == 409, "跨 Agent 复用同 Key 必须冲突"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_id04_same_key_different_session_409(self, client, db_setup, monkeypatch):
        # §9.3-4：相同 Key、不同 session_id → 冲突
        _patch_model(monkeypatch, [AIMessage(content="S1_OK"), AIMessage(content="S2_OK")])
        key = f"idem-{uuid.uuid4().hex}"
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": key}

        r1 = await client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/chat",
            json=_chat_payload("第一次"), headers=headers,
        )
        assert r1.status_code == 200
        session1_id = r1.json()["session_id"]
        assert session1_id

        r2 = await client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/sessions",
            json={}, headers=_auth(db_setup["user1_id"]),
        )
        assert r2.status_code == 200
        session2_id = r2.json()["id"]

        r3 = await client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/chat",
            json=_chat_payload("第一次", session_id=session2_id), headers=headers,
        )
        assert r3.status_code == 409, "跨 Session 复用同 Key 必须冲突"
        assert session1_id != session2_id

    @pytest.mark.asyncio(loop_scope="session")
    async def test_id05_stream_retry_still_returns_sse(self, client, db_setup, monkeypatch):
        # §9.3-5：同一个流式请求重试后仍然返回 SSE（幂等命中不得改为普通 JSON）
        _patch_model(monkeypatch, [AIMessage(content="SSE_OK")])
        key = f"idem-{uuid.uuid4().hex}"
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": key}
        body = _chat_payload("流式幂等", stream=True)

        async with client.stream(
            "POST", f"/api/v1/agents/{db_setup['agent_id']}/chat", json=body, headers=headers
        ) as resp:
            assert resp.status_code == 200
            run_id = resp.headers.get("x-run-id")
            assert run_id
            events1 = await _iter_sse(resp)
        assert events1[-1]["type"] == "done"

        async with client.stream(
            "POST", f"/api/v1/agents/{db_setup['agent_id']}/chat", json=body, headers=headers
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers.get("content-type", "").startswith("text/event-stream"), \
                "幂等命中必须保持 SSE 协议"
            assert resp.headers.get("x-run-id") == run_id
            events2 = await _iter_sse(resp)

        assert events2, "幂等回放应包含持久化事件"
        assert events2[-1]["type"] == "done", "回放流必须以 done 终止"
        assert {e["type"] for e in events2} >= {"final"}

    @pytest.mark.asyncio(loop_scope="session")
    async def test_id06_no_duplicate_user_message(self, client, db_setup, monkeypatch):
        # §9.3-6：同一个请求重试不会重复追加用户消息
        _patch_model(monkeypatch, [AIMessage(content="MSG_OK")])
        key = f"idem-{uuid.uuid4().hex}"
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": key}
        body = _chat_payload("只允许出现一次的用户消息")

        session_id = None
        for _ in range(3):
            r = await client.post(
                f"/api/v1/agents/{db_setup['agent_id']}/chat", json=body, headers=headers
            )
            assert r.status_code == 200
            session_id = session_id or r.json().get("session_id")

        r = await client.get(
            f"/api/v1/agents/{db_setup['agent_id']}/sessions/{session_id}/messages",
            headers=_auth(db_setup["user1_id"]),
        )
        assert r.status_code == 200
        msgs = r.json()["messages"]
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert len(user_msgs) == 1, f"幂等重试重复追加了用户消息: {user_msgs}"
        assert user_msgs[0]["content"] == "只允许出现一次的用户消息"
        # 每条消息带稳定身份（P1-02 B）
        assert user_msgs[0].get("message_id"), "用户消息缺少稳定 message_id"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_id07_concurrent_same_key_no_orphan_session(self, db_setup, monkeypatch):
        # §9.3-7：并发请求不会产生孤立 Session（同事务收敛于 Run 唯一约束）
        record = []
        gate = asyncio.Event()
        gate.set()  # 立即完成
        monkeypatch.setattr(
            AgentRuntimeService, "_drive", _gate_drive(record, gate)
        )
        svc = _make_service()
        key = f"idem-{uuid.uuid4().hex}"
        user_message = {"role": "user", "content": "并发幂等"}
        messages = [user_message]
        cfg = {
            "id": db_agent_id(), "system_prompt": "x", "max_iterations": 3,
            "max_tokens": 512, "temperature": "0.7", "tools": [],
        }
        uid = db_user_id()
        sessions_before = await _count(AgentSession)

        results = await asyncio.gather(
            svc.start_run_with_session(
                agent_config=cfg, messages=messages, user_id=uid, session_id=None,
                idempotency_key=key, user_message=user_message,
            ),
            svc.start_run_with_session(
                agent_config=cfg, messages=messages, user_id=uid, session_id=None,
                idempotency_key=key, user_message=user_message,
            ),
            return_exceptions=True,
        )
        errs = [r for r in results if isinstance(r, Exception)]
        assert not errs, f"并发幂等请求不应抛出: {errs}"
        run_ids = {r[0] for r in results}
        assert len(run_ids) == 1, f"并发同 Key 应收敛为同一 Run: {run_ids}"

        # 不产生孤立 Session：只新增 1 个会话（后到者的创建随事务回滚）
        sessions_after = await _count(AgentSession)
        assert sessions_after - sessions_before == 1, "并发幂等请求产生了孤立 Session"

        # 用户消息只追加一条
        session_id = results[0][1]
        async with AsyncSessionLocal() as s:
            msgs = await SessionRepository(s).get_messages(session_id)
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert len(user_msgs) == 1, f"并发幂等请求重复追加用户消息: {user_msgs}"

        with sync_engine.begin() as conn:
            rows = conn.execute(
                select(AgentRun.id).where(AgentRun.idempotency_key == key)
            ).all()
        assert len(rows) == 1


# ═════════════ §10.3：P1-02 Session 并发锁 / 消息一致性 / Outbox 写回验收 ══════════


class TestSessionConcurrencyOutbox:
    """§10.3 验收：Session 执行权 / 消息乐观锁 / Run 终态与写回解耦。"""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_sd01_same_session_two_runs_single_execution_right(self, db_setup, monkeypatch):
        # §10.3-1：同一个 Session 两个 Run 并发提交时只有一个获得执行权
        async with AsyncSessionLocal() as s:
            sess = AgentSession(agent_id=db_agent_id(), user_id=db_user_id(), messages=[])
            s.add(sess)
            await s.flush()
            sid = sess.id
            await s.commit()

        # a) 仓库级 CAS：并发 Claim 只有一个成功
        async def _claim(run_id: str) -> bool:
            async with AsyncSessionLocal() as s2:
                ok = await SessionRepository(s2).claim_execution(sid, run_id, 30)
                await s2.commit()
                return ok

        r1, r2 = await asyncio.gather(_claim("run-cas-a"), _claim("run-cas-b"))
        assert sorted([r1, r2]) == [False, True], "并发 Claim 必须只有一个赢家"

        # b) 服务级：Run1 持有执行权期间，Run2 明确冲突（不并发驱动同一 Session）
        async with AsyncSessionLocal() as s3:
            await SessionRepository(s3).release_execution(
                sid, "run-cas-a" if r1 else "run-cas-b"
            )
            await s3.commit()

        record: List[Dict] = []
        gate = asyncio.Event()
        monkeypatch.setattr(AgentRuntimeService, "_drive", _gate_drive(record, gate))
        svc = _make_service()
        cfg = _agent_config()
        uid = db_user_id()

        run1_id, _sid, _ = await svc.start_run_with_session(
            agent_config=cfg, messages=[{"role": "user", "content": "run1"}],
            user_id=uid, session_id=sid,
            idempotency_key=f"k1-{uuid.uuid4().hex}",
        )
        with pytest.raises(SessionBusy):
            await svc.start_run_with_session(
                agent_config=cfg, messages=[{"role": "user", "content": "run2"}],
                user_id=uid, session_id=sid,
                idempotency_key=f"k2-{uuid.uuid4().hex}",
            )
        # 未获得执行权的 Run 不得留下孤儿行（同事务回滚）
        assert await _count(AgentRun) == 1

        gate.set()
        row = await _wait_status(run1_id, [STATUS_COMPLETED])
        assert row is not None and row.status == STATUS_COMPLETED
        # Run 终态释放 Session 执行权
        async with AsyncSessionLocal() as s4:
            sess_row = await s4.get(AgentSession, sid)
        assert sess_row.active_run_id is None, "终态必须释放 Session 执行权"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_sd02_two_sessions_run_in_parallel(self, db_setup, monkeypatch):
        # §10.3-2：两个 Session 之间仍可并行执行
        async with AsyncSessionLocal() as s:
            s1 = AgentSession(agent_id=db_agent_id(), user_id=db_user_id(), messages=[])
            s2 = AgentSession(agent_id=db_agent_id(), user_id=db_user_id(), messages=[])
            s.add_all([s1, s2])
            await s.flush()
            sid1, sid2 = s1.id, s2.id
            await s.commit()

        record: List[Dict] = []
        gate = asyncio.Event()
        monkeypatch.setattr(AgentRuntimeService, "_drive", _gate_drive(record, gate))
        svc = _make_service()
        cfg = _agent_config()
        uid = db_user_id()

        run1_id, _, _ = await svc.start_run_with_session(
            agent_config=cfg, messages=[{"role": "user", "content": "s1"}],
            user_id=uid, session_id=sid1, idempotency_key=f"k-{uuid.uuid4().hex}",
        )
        run2_id, _, _ = await svc.start_run_with_session(
            agent_config=cfg, messages=[{"role": "user", "content": "s2"}],
            user_id=uid, session_id=sid2, idempotency_key=f"k-{uuid.uuid4().hex}",
        )
        # 两个 Session 同时持有执行权（并行执行互不阻塞）
        async with AsyncSessionLocal() as s:
            rows = [await s.get(AgentSession, sid1), await s.get(AgentSession, sid2)]
        assert rows[0].active_run_id == run1_id
        assert rows[1].active_run_id == run2_id

        gate.set()
        r1 = await _wait_status(run1_id, [STATUS_COMPLETED])
        r2 = await _wait_status(run2_id, [STATUS_COMPLETED])
        assert r1 is not None and r2 is not None, "两个 Session 的 Run 应都能执行完成"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_sd03_writeback_failure_keeps_terminal_state(self, db_setup, monkeypatch):
        # §10.3-3：Assistant 写回失败后 Run 仍保持正确终态（Run 终态与写回解耦）
        fake = _patch_model(monkeypatch, [AIMessage(content="WB_FAIL")])

        async def fail_append2(self, session_id, new_messages, **kw):
            if kw.get("source_run_id"):
                raise RuntimeError("模拟写回故障")
            # 非写回路径（用户消息）正常执行
            return await orig_append(self, session_id, new_messages, **kw)

        orig_append = SessionRepository.append_messages
        monkeypatch.setattr(SessionRepository, "append_messages", fail_append2)

        svc = _make_service()
        async with AsyncSessionLocal() as s:
            sess = AgentSession(agent_id=db_agent_id(), user_id=db_user_id(), messages=[])
            s.add(sess)
            await s.flush()
            sid = sess.id
            await s.commit()

        user_message = {"role": "user", "content": "写回失败不影响终态"}
        result = await svc.run_to_completion(
            agent_config=_agent_config(),
            messages=[user_message],
            user_id=db_user_id(),
            session_id=sid,
            idempotency_key=f"k-{uuid.uuid4().hex}",
            user_message=user_message,
        )
        run_id = result["run_id"]

        row = await _wait_status(run_id, [STATUS_COMPLETED])
        assert row is not None, "写回失败不得改变 Run 终态（应保持 completed）"
        assert row.status == STATUS_COMPLETED

        # 写回任务留在 outbox（pending + attempts 已记录），可重试
        with sync_engine.begin() as conn:
            row = conn.execute(
                select(SessionResultOutbox.status, SessionResultOutbox.attempts)
                .where(SessionResultOutbox.run_id == run_id)
            ).one()
        assert row[0] == "pending"
        assert row[1] >= 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_sd04_outbox_retry_no_duplicate_message(self, db_setup, monkeypatch):
        # §10.3-4：Session 写回任务重试 3 次只追加一条 Assistant 消息
        _patch_model(monkeypatch, [AIMessage(content="RETRY_OK")])

        # 注入：前两次 mark_delivered 失败（写回事务整体回滚 → 重试不得重复追加）
        orig_mark = SessionOutboxRepository.mark_delivered
        failures = {"n": 0}

        async def flaky_mark(self, run_id):
            if failures["n"] < 2:
                failures["n"] += 1
                raise RuntimeError("模拟 delivered 标记故障")
            return await orig_mark(self, run_id)

        monkeypatch.setattr(SessionOutboxRepository, "mark_delivered", flaky_mark)

        svc = _make_service()
        async with AsyncSessionLocal() as s:
            sess = AgentSession(agent_id=db_agent_id(), user_id=db_user_id(), messages=[])
            s.add(sess)
            await s.flush()
            sid = sess.id
            await s.commit()

        user_message = {"role": "user", "content": "写回重试"}
        result = await svc.run_to_completion(
            agent_config=_agent_config(),
            messages=[user_message],
            user_id=db_user_id(),
            session_id=sid,
            idempotency_key=f"k-{uuid.uuid4().hex}",
            user_message=user_message,
        )
        run_id = result["run_id"]

        # 第 1 次写回在终态路径失败（调用点吞掉记日志），后续由 Outbox Worker 重试
        assert await _wait_status(run_id, [STATUS_COMPLETED]) is not None
        assert failures["n"] == 1

        await svc.deliver_pending_outbox()  # 第 2 次：仍失败
        assert failures["n"] == 2
        await svc.deliver_pending_outbox()  # 第 3 次：成功
        assert failures["n"] == 2

        # 重试 3 次只追加一条 Assistant 消息
        async with AsyncSessionLocal() as s:
            msgs = await SessionRepository(s).get_messages(sid)
        assistant = [m for m in msgs if m.get("role") == "assistant"]
        assert len(assistant) == 1, f"重试重复追加了 Assistant 消息: {assistant}"
        assert assistant[0].get("source_run_id") == run_id
        assert assistant[0].get("message_id"), "Assistant 消息缺少稳定 message_id"

        with sync_engine.begin() as conn:
            row = conn.execute(
                select(SessionResultOutbox.status, SessionResultOutbox.attempts)
                .where(SessionResultOutbox.run_id == run_id)
            ).one()
        assert row[0] == "delivered"
        assert row[1] == 2  # 两次失败记录

        # 再次投递（幂等）：不得追加第二条
        await svc.deliver_pending_outbox()
        async with AsyncSessionLocal() as s:
            msgs = await SessionRepository(s).get_messages(sid)
        assert len([m for m in msgs if m.get("role") == "assistant"]) == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_sd05_concurrent_user_message_writes_no_loss(self, db_setup):
        # §10.3-5：并发用户消息写入不会丢失历史内容（version 乐观锁合并）
        async with AsyncSessionLocal() as s:
            sess = AgentSession(agent_id=db_agent_id(), user_id=db_user_id(), messages=[])
            s.add(sess)
            await s.flush()
            sid = sess.id
            await s.commit()

        m1 = {"message_id": uuid.uuid4().hex, "role": "user", "content": "并发消息一"}
        m2 = {"message_id": uuid.uuid4().hex, "role": "user", "content": "并发消息二"}

        async def _append(msg):
            async with AsyncSessionLocal() as s2:
                await SessionRepository(s2).append_messages(sid, [msg])
                await s2.commit()

        await asyncio.gather(_append(m1), _append(m2))

        async with AsyncSessionLocal() as s:
            msgs = await SessionRepository(s).get_messages(sid)
            sess_row = await s.get(AgentSession, sid)
        contents = {m.get("content") for m in msgs}
        assert contents == {"并发消息一", "并发消息二"}, f"并发写入丢消息: {msgs}"
        assert sess_row.version == 2, "每次写入应递增 version"
        # 消息带稳定身份
        assert all(m.get("message_id") for m in msgs)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_sd06_outbox_recoverable_after_restart(self, db_setup, monkeypatch):
        # §10.3-6：服务重启后未完成的 Session 写回可以恢复（Outbox 驱动）
        _patch_model(monkeypatch, [AIMessage(content="RECOVERED")])

        # 模拟进程在写回前崩溃：终态已提交 + outbox 已入队，但写回未执行
        async def crashed_write_back(self, run):
            return None

        monkeypatch.setattr(AgentRuntimeService, "_write_back_session", crashed_write_back)

        svc = _make_service(worker_id="w-before-restart")
        async with AsyncSessionLocal() as s:
            sess = AgentSession(agent_id=db_agent_id(), user_id=db_user_id(), messages=[])
            s.add(sess)
            await s.flush()
            sid = sess.id
            await s.commit()

        user_message = {"role": "user", "content": "重启恢复"}
        result = await svc.run_to_completion(
            agent_config=_agent_config(),
            messages=[user_message],
            user_id=db_user_id(),
            session_id=sid,
            idempotency_key=f"k-{uuid.uuid4().hex}",
            user_message=user_message,
        )
        run_id = result["run_id"]
        assert await _wait_status(run_id, [STATUS_COMPLETED]) is not None

        # 崩溃现场：outbox pending、Session 尚无 Assistant 消息
        with sync_engine.begin() as conn:
            row = conn.execute(
                select(SessionResultOutbox.status)
                .where(SessionResultOutbox.run_id == run_id)
            ).one()
        assert row[0] == "pending"
        async with AsyncSessionLocal() as s:
            msgs = await SessionRepository(s).get_messages(sid)
        assert not [m for m in msgs if m.get("role") == "assistant"]

        # “重启”后：新服务实例恢复扫描 pending 写回
        svc2 = _make_service(worker_id="w-after-restart")
        delivered = await svc2.deliver_pending_outbox()
        assert delivered == 1

        async with AsyncSessionLocal() as s:
            msgs = await SessionRepository(s).get_messages(sid)
        assistant = [m for m in msgs if m.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"] == "RECOVERED"
        assert assistant[0].get("source_run_id") == run_id

        with sync_engine.begin() as conn:
            row = conn.execute(
                select(SessionResultOutbox.status)
                .where(SessionResultOutbox.run_id == run_id)
            ).one()
        assert row[0] == "delivered"


# ── 共用小工具 ───────────────────────────────────────────────────────────────


def _agent_config() -> Dict[str, Any]:
    return {
        "id": db_agent_id(),
        "model_config_id": db_model_config_id(),
        "system_prompt": "x",
        "max_iterations": 3,
        "max_tokens": 512,
        "temperature": "0.7",
        "tools": [],
    }


def db_agent_id() -> int:
    with sync_engine.begin() as conn:
        return conn.execute(
            select(Agent.id).where(Agent.name == "b5-agent")
        ).scalar_one()


def db_model_config_id() -> int:
    from app.models.model_provider import ModelConfig

    with sync_engine.begin() as conn:
        return conn.execute(
            select(ModelConfig.id).where(ModelConfig.name == "b5-model")
        ).scalar_one()


def db_user_id() -> int:
    with sync_engine.begin() as conn:
        return conn.execute(
            select(User.id).where(User.username == "b5-alice")
        ).scalar_one()

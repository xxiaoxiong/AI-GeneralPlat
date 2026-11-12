"""API 级集成冒烟（FastAPI TestClient + SQLite 业务库 + FakeChatModel）。

覆盖任务书 §8 验收场景的 HTTP 级证据：
- AT-01：创建 Agent、/chat SSE、/invoke 非流式、会话历史查询、删除会话
- AT-03：/chat 真流式事件（final_chunk/final/done + X-Run-Id 头）
- P0-G：SSE 事件 seq 序号 + GET /agents/runs/{run_id} 断连查询（AT-03）
- AT-08：任务生命周期（超限/超时/空结果/取消的明确状态）
- AT-15：越权访问他人 Agent/会话被拒（所有权校验）
- AT-16：工作流 Agent 节点（agent_invoke）端到端
"""
import json
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import create_access_token


class FakeChatModel(BaseChatModel):
    """脚本化模型（API 冒烟用，无网络）。"""

    script: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake-api"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        item = self.script.pop(0) if self.script else AIMessage(content="（脚本结束）")
        msg = item() if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _tool_call_turn(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def db_setup():
    """创建两个用户 + 模型配置 + Agent（A 属用户 1）。"""
    import os

    from app.models.agent import Agent
    from app.models.model_provider import ModelConfig, ModelProvider
    from app.models.user import User

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        u1 = User(username="api_u1", email="u1@test.dev", hashed_password="x", is_active=True)
        u2 = User(username="api_u2", email="u2@test.dev", hashed_password="x", is_active=True)
        s.add_all([u1, u2])
        s.flush()
        provider = ModelProvider(
            name="fake-provider", display_name="Fake", provider_type="openai_compatible",
            base_url="http://fake.local/v1", api_key="none", is_active=True,
        )
        s.add(provider)
        s.flush()
        mc = ModelConfig(
            provider_id=provider.id, name="fake-api", display_name="Fake API",
            model_type="chat", is_active=True, supports_streaming=True,
            supports_function_call=True,
        )
        s.add(mc)
        s.flush()
        agent = Agent(
            name="冒烟 Agent", description="api smoke", model_config_id=mc.id,
            system_prompt="你是冒烟测试助手。", tools=["calculator"], is_active=True,
            owner_id=u1.id,
        )
        s.add(agent)
        s.commit()
        return {"user1_id": u1.id, "user2_id": u2.id, "agent_id": agent.id, "model_config_id": mc.id}


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _patch_model(monkeypatch, script):
    import app.services.agent_runtime.factory as factory_mod

    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: FakeChatModel(script=list(script)))


def _iter_sse(resp):
    """解析 data: {json} 行为 dict 列表。"""
    events = []
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


# ── AT-01 / AT-03：/chat 流式对话 ─────────────────────────────────────────

class TestChatFlow:
    def test_chat_sse_stream(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [
            _tool_call_turn("calculator", {"expression": "21*2"}),
            AIMessage(content="SSE_DONE_42"),
        ])
        with client.stream(
            "POST",
            f"/api/v1/agents/{db_setup['agent_id']}/chat",
            json={"message": "计算 21*2", "stream": True, "use_memory": False},
            headers=_auth(db_setup["user1_id"]),
        ) as resp:
            assert resp.status_code == 200
            # X-Run-Id 与 X-Session-Id 响应头（P0-G 重连查询）
            assert resp.headers.get("x-run-id"), "缺少 X-Run-Id 响应头"
            assert resp.headers.get("x-session-id"), "缺少 X-Session-Id 响应头"
            events = _iter_sse(resp)

        types = [e["type"] for e in events]
        assert "action" in types, f"缺少工具调用事件: {types}"
        assert "observation" in types, f"缺少工具结果事件: {types}"
        assert "final_chunk" in types and "final" in types and "done" in types
        assert events[-1]["type"] == "done" and events[-1]["status"] == "completed"
        final = [e for e in events if e["type"] == "final"][0]
        assert "SSE_DONE_42" in final["content"], f"最终内容缺失: {final}"
        action = [e for e in events if e["type"] == "action"][0]
        assert action["tool"] == "calculator"
        # seq 序号连续（P0-G）
        seqs = [e["seq"] for e in events if "seq" in e]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"seq 异常: {seqs}"

        # P0-G：run 状态可查询（断连后恢复/轮询，AT-03）
        run_id = resp.headers["x-run-id"]
        r = client.get(f"/api/v1/agents/runs/{run_id}", headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed", body

    def test_chat_without_tools_non_stream_invoke(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="INVOKE_OK")])
        r = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/invoke",
            json={"input": "你好"},
            headers=_auth(db_setup["user1_id"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["content"] == "INVOKE_OK"
        assert body.get("run_id"), "invoke 响应应带 run_id"

    def test_invoke_idempotency_key_hits_original_run(self, client, db_setup, monkeypatch):
        """Idempotency-Key：幂等命中返回原 Run ID 与状态，不重复执行（FR-01 / §8.2）。"""
        _patch_model(monkeypatch, [AIMessage(content="IDEM_OK")])
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": "idem-1"}
        r1 = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/invoke",
            json={"input": "幂等测试"},
            headers=headers,
        )
        assert r1.status_code == 200 and r1.json()["content"] == "IDEM_OK"
        run_id = r1.json()["run_id"]

        # 同键重试：命中缓存（模型脚本已耗尽，若重复执行必然失败）
        r2 = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/invoke",
            json={"input": "幂等测试"},
            headers=headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["idempotent_reuse"] is True
        assert body2["run_id"] == run_id
        assert body2["content"] == "IDEM_OK"

        # 不同键 → 新 Run
        _patch_model(monkeypatch, [AIMessage(content="IDEM_OK2")])
        headers2 = {**_auth(db_setup["user1_id"]), "Idempotency-Key": "idem-2"}
        r3 = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/invoke",
            json={"input": "幂等测试"},
            headers=headers2,
        )
        assert r3.status_code == 200 and r3.json()["run_id"] != run_id

    def test_chat_idempotency_key_no_duplicate_execution(self, client, db_setup, monkeypatch):
        """/chat 同键重试：不重复执行、返回原 Run（FR-01 / §8.2）。"""
        _patch_model(monkeypatch, [AIMessage(content="CHAT_IDEM")])
        headers = {**_auth(db_setup["user1_id"]), "Idempotency-Key": "idem-chat"}
        payload = {"message": "幂等对话", "stream": False, "use_memory": False}
        c1 = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/chat", json=payload, headers=headers,
        )
        assert c1.status_code == 200 and c1.json()["content"] == "CHAT_IDEM"
        c2 = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/chat", json=payload, headers=headers,
        )
        assert c2.status_code == 200
        assert c2.json()["idempotent_reuse"] is True

    def test_session_history_and_delete(self, client, db_setup):
        # 创建会话并对话
        _id = db_setup["agent_id"]
        with client.stream(
            "POST", f"/api/v1/agents/{_id}/chat",
            json={"message": "记录一下", "stream": True, "use_memory": False},
            headers=_auth(db_setup["user1_id"]),
        ) as resp:
            events = _iter_sse(resp)
        session_id = int(resp.headers["x-session-id"])

        # 历史查询（AT-01）
        r = client.get(f"/api/v1/agents/{_id}/sessions/{session_id}/messages",
                       headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 200
        msgs = r.json()
        assert isinstance(msgs, (list, dict))

        # 删除会话后不可再查（AT-01）
        r = client.delete(f"/api/v1/agents/{_id}/sessions/{session_id}",
                          headers=_auth(db_setup["user1_id"]))
        assert r.status_code in (200, 204)
        r = client.get(f"/api/v1/agents/{_id}/sessions/{session_id}/messages",
                       headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 404


# ── AT-15：越权拒绝 ─────────────────────────────────────────────────────────

class TestOwnership:
    def test_other_user_cannot_access_agent(self, client, db_setup):
        _id = db_setup["agent_id"]
        other = _auth(db_setup["user2_id"])
        assert client.get(f"/api/v1/agents/{_id}", headers=other).status_code == 404
        assert client.post(f"/api/v1/agents/{_id}/invoke",
                           json={"input": "x"}, headers=other).status_code == 404
        with client.stream(
            "POST", f"/api/v1/agents/{_id}/chat",
            json={"message": "x", "stream": True}, headers=other,
        ) as resp:
            assert resp.status_code == 404

    def test_other_user_cannot_query_run(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="PRIVATE")])
        r = client.post(f"/api/v1/agents/{db_setup['agent_id']}/invoke",
                        json={"input": "hi"}, headers=_auth(db_setup["user1_id"]))
        run_id = r.json()["run_id"]
        assert client.get(f"/api/v1/agents/runs/{run_id}",
                          headers=_auth(db_setup["user2_id"])).status_code in (403, 404)


# ── AT-16：工作流 Agent 节点端到端 ──────────────────────────────────────────

class TestWorkflowAgentNode:
    def test_workflow_agent_invoke_node(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [
            _tool_call_turn("calculator", {"expression": "6*7"}),
            AIMessage(content="WF_DONE_42"),
        ])
        definition = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "agent1", "type": "agent_invoke",
                 "config": {"agent_id": db_setup["agent_id"], "input": "{{text}}"}},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"from": "start", "to": "agent1"},
                {"from": "agent1", "to": "end"},
            ],
        }
        r = client.post("/api/v1/workflows",
                        json={"name": "Agent 节点冒烟", "definition": definition},
                        headers=_auth(db_setup["user1_id"]))
        assert r.status_code in (200, 201), r.text
        wf_id = r.json()["id"]

        r = client.post(f"/api/v1/workflows/{wf_id}/execute",
                        json={"input_data": {"text": "计算 6*7"}},
                        headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "success", out
        assert not out.get("error_msg")
        node_results = out.get("node_results") or {}
        dumped = json.dumps(node_results, ensure_ascii=False)
        assert "WF_DONE_42" in dumped, f"Agent 节点输出缺失: {dumped[:500]}"


# ── AT-08 / P0-A：任务生命周期（预算超限/超时/空结果/取消）─────────────────

class TestBudgetAndLifecycle:
    def test_tool_budget_exceeded_status(self, client, db_setup, monkeypatch):
        """工具调用次数超限 -> 明确 limit_exceeded 状态（与 failed/成功区分）"""
        import app.core.config as config_mod
        monkeypatch.setattr(config_mod.settings, "AGENT_MAX_TOOL_CALLS", 2, raising=False)

        counter = {"n": 0}

        def endless_tool_call(**kw):
            counter["n"] += 1
            return _tool_call_turn("calculator", {"expression": "1+1"}, f"tc{counter['n']}")

        _patch_model(monkeypatch, [endless_tool_call] * 30)
        r = client.post(f"/api/v1/agents/{db_setup['agent_id']}/invoke",
                        json={"input": "一直计算"},
                        headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "limit_exceeded", data
        info = client.get(f"/api/v1/agents/runs/{data['run_id']}",
                          headers=_auth(db_setup["user1_id"])).json()
        assert "预算" in (info.get("error") or ""), info

    def test_run_timeout_status(self, client, db_setup, monkeypatch):
        """运行超时 -> limit_exceeded，错误信息含超时"""
        import time as _time

        import app.core.config as config_mod
        monkeypatch.setattr(config_mod.settings, "AGENT_RUN_TIMEOUT", 0.3, raising=False)

        def slow_answer(**kw):
            _time.sleep(0.8)
            return AIMessage(content="迟到的回答")

        _patch_model(monkeypatch, [slow_answer])
        r = client.post(f"/api/v1/agents/{db_setup['agent_id']}/invoke",
                        json={"input": "慢一点"},
                        headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "limit_exceeded", data
        info = client.get(f"/api/v1/agents/runs/{data['run_id']}",
                          headers=_auth(db_setup["user1_id"])).json()
        assert "超时" in (info.get("error") or ""), info

    def test_empty_result_is_success(self, client, db_setup, monkeypatch):
        """AT-08：空结果是正常完成，不当错误"""
        _patch_model(monkeypatch, [AIMessage(content="")])
        r = client.post(f"/api/v1/agents/{db_setup['agent_id']}/invoke",
                        json={"input": "什么都不说"},
                        headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok", data
        assert data["content"] == ""

    @pytest.mark.asyncio
    async def test_cancel_not_misreported_as_success(self, client, db_setup, monkeypatch):
        """AT-08：取消 -> cancelled（done.status=cancelled），不被误报成功"""
        import asyncio

        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult

        from app.services.agent_runtime import get_runtime_service

        gate: Any = asyncio.Event()
        entered: Any = asyncio.Event()

        class HangingChatModel(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "hang"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                raise RuntimeError("同步路径不使用")

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                entered.set()
                await gate.wait()
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="late"))])

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "p", "base_url": "http://x", "api_key": "k", "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: HangingChatModel())

        svc = get_runtime_service()
        run_id, stream = await svc.start_run(
            agent_config={"id": 990001, "tools": ["calculator"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "hi"}],
            user_id=db_setup["user1_id"],
        )
        await asyncio.wait_for(entered.wait(), timeout=10)
        res = await svc.cancel_run(run_id, db_setup["user1_id"])
        assert res["ok"], res

        events = [e async for e in stream]
        done = [e for e in events if e.get("type") == "done"]
        assert done and done[-1]["status"] == "cancelled", done
        info = await svc.get_run(run_id, db_setup["user1_id"])
        assert info["status"] == "cancelled"


# ── FR-07：SSE 事件回放 / 断线续传 ─────────────────────────────────────────


class TestRunEventsReplay:
    def test_events_replay_after_seq_and_last_event_id(self, client, db_setup, monkeypatch):
        """GET /runs/{id}/events：旧契约字段保留 + seq 连续 + after_seq/Last-Event-ID 续传"""
        _patch_model(monkeypatch, [AIMessage(content="HELLO_EVT")])
        resp = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/invoke",
            json={"input": "hi"},
            headers=_auth(db_setup["user1_id"]),
        )
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        def fetch(after_seq=None, last_event_id=None):
            params = {"after_seq": after_seq} if after_seq is not None else {}
            headers = dict(_auth(db_setup["user1_id"]))
            if last_event_id is not None:
                headers["Last-Event-ID"] = str(last_event_id)
            r = client.get(
                f"/api/v1/agents/runs/{run_id}/events", params=params, headers=headers,
            )
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            return r

        # 全量回放：data 为旧契约 JSON（type 等字段保留）+ seq 连续 + 终态 done
        r = fetch()
        assert "id: " in r.text  # 标准 SSE id 行
        events = _iter_sse(r)
        assert events and events[-1]["type"] == "done"
        assert [e["seq"] for e in events] == list(range(len(events)))
        assert all("type" in e for e in events)

        # after_seq 续传：只返回 seq 大于指定值的事件，直至终态
        resumed = _iter_sse(fetch(after_seq=events[0]["seq"]))
        assert [e["seq"] for e in resumed] == [e["seq"] for e in events][1:]
        assert resumed[-1]["type"] == "done"

        # Last-Event-ID（标准 SSE 重连头）与 after_seq 等价
        by_header = _iter_sse(fetch(last_event_id=events[0]["seq"]))
        assert [e["seq"] for e in by_header] == [e["seq"] for e in resumed]

    def test_events_replay_forbidden_for_other_user(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="x")])
        resp = client.post(
            f"/api/v1/agents/{db_setup['agent_id']}/invoke",
            json={"input": "hi"},
            headers=_auth(db_setup["user1_id"]),
        )
        run_id = resp.json()["run_id"]
        r = client.get(
            f"/api/v1/agents/runs/{run_id}/events",
            headers=_auth(db_setup["user2_id"]),
        )
        assert r.status_code in (403, 404)

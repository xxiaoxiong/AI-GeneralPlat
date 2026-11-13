"""P0-05 工具执行结果语义统一（任务书 §7.3 验收，Batch 3）。

逐条对应 §7.3 六项验收：
  1. Handler 返回业务错误时 Ledger 不得标记 succeeded；
  2. HTTP 500 不得被无条件记录为成功；
  3. 邮件服务器连接中断等不确定情况进入 unknown；
  4. Webhook 超时不会被当作成功；
  5. 所有工具均经过统一结果归一化；
  6. 长时间 SMTP 调用不会阻塞 Worker Heartbeat。

运行：
    pytest backend/tests/test_tool_result_semantics.py -q
"""
import asyncio
import json
import socket
import threading
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, update

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import (
    Agent,
    AgentApproval,
    AgentRun,
    AgentRunEvent,
    AgentToolExecution,
)
from app.services.agent_runtime.context import RunScope, RuntimeContext
from app.services.agent_runtime.events import (
    TOOL_COMPLETED,
    TOOL_FAILED,
    EventBus,
    RuntimeEvent,
)
from app.services.agent_runtime.lifecycle import RunExecutionLease
from app.services.agent_runtime.repositories import (
    RunRepository,
    ToolExecutionRepository,
)
from app.services.agent_runtime.repositories.tool_execution_repository import (
    TOOL_EXEC_FAILED,
    TOOL_EXEC_SUCCEEDED,
    TOOL_EXEC_UNKNOWN,
)
from app.services.agent_runtime.tools.adapter import ToolAdapterFactory
from app.services.agent_runtime.tools.policy import ToolPolicy
from app.services.tools import ToolExecutor
from app.services.tools import notify_tools
from app.services.tools.network_tools import execute_http_get, execute_http_post
from app.services.tools.notify_tools import (
    execute_send_email_tool,
    execute_webhook_notify,
)
from app.services.tools.result import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    ToolResult,
    normalize,
)

import smtplib


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in (AgentRunEvent, AgentToolExecution, AgentApproval, AgentRun):
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


def _uid() -> str:
    return uuid.uuid4().hex


async def _create_run(repo: RunRepository, **kw):
    run_id = _uid()
    agent = await repo.session.get(Agent, 1)
    if agent is None:
        agent = Agent(id=1, name="sem-agent")
        repo.session.add(agent)
        await repo.session.flush()
    defaults = dict(
        run_id=run_id,
        agent_id=1,
        session_id=None,
        user_id=None,
        thread_id=f"agent-1-session-{run_id[:8]}",
    )
    defaults.update(kw)
    return await repo.create_run(**defaults)


async def _claimed_run(worker_id: str = "w-tool"):
    async with AsyncSessionLocal() as s:
        repo = RunRepository(s)
        run, _ = await _create_run(repo)
        version = await repo.claim(run.id, worker_id, 30)
        await s.commit()
    return run.id, version


def _lease_for(run_id: str, version: int, worker_id: str = "w-tool") -> RunExecutionLease:
    return RunExecutionLease(run_id=run_id, worker_id=worker_id, lease_version=version)


def _make_ctx(run_id: str, worker_id: str = "w-tool", lease_version: int = 1) -> RuntimeContext:
    return RuntimeContext(
        scope=RunScope(user_id=1),
        run_id=run_id,
        thread_id=f"thr-{run_id}",
        agent_id=1,
        worker_id=worker_id,
        lease_version=lease_version,
    )


class _RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.published: list = []

    async def publish(self, ev: RuntimeEvent) -> None:
        self.published.append(ev.type)
        await super().publish(ev)


def _make_adapter(ctx: RuntimeContext, tool_names, bus=None) -> ToolAdapterFactory:
    policy = ToolPolicy.from_settings(set(tool_names), enable_hitl=False)
    return ToolAdapterFactory(ctx, policy, bus or EventBus())


# ── HTTP 替身（不触网，语义按返回状态码/异常精确注入） ──────────────────────


def _stub_public_dns(monkeypatch):
    """SSRF 检查做真实 DNS 解析：测试环境可能无外网，stub 为公网 IP。"""

    def _getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data, ensure_ascii=False) if json_data is not None else "")
        self.is_success = 200 <= status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _fake_httpx(monkeypatch, *, response: _FakeResponse = None, exc: Exception = None):
    """替换 httpx.AsyncClient：固定返回 response 或抛 exc。"""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if exc is not None:
                raise exc
            return response

        async def post(self, *a, **k):
            if exc is not None:
                raise exc
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


# ════════════════════════════════════════════════════════════════════════════
# 验收 1：Handler 返回业务错误时 Ledger 不得标记 succeeded
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handler_business_error_not_marked_succeeded(monkeypatch):
    """业务错误（ToolResult.failed）-> 账本 failed，绝不 succeeded、不发 TOOL_COMPLETED。"""
    run_id, v1 = await _claimed_run()
    bus = _RecordingBus()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=v1), ["webhook_notify"], bus)

    async def behavior(name, args):
        return ToolResult.failed("errcode=3 业务规则拒绝")

    async def _exec(self, name, args, *, raise_on_error=False):
        return await behavior(name, args)

    monkeypatch.setattr(ToolExecutor, "execute", _exec)

    text = await adapter._execute("webhook_notify", {
        "webhook_url": "https://example.com/hook", "message": "m", "tool_call_id": "tc-biz",
    })
    assert "errcode=3" in text

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-biz")
    assert rec.status == TOOL_EXEC_FAILED, "业务错误不得标记 succeeded"
    assert rec.result_payload is None
    assert TOOL_COMPLETED not in bus.published
    assert TOOL_FAILED in bus.published


@pytest.mark.asyncio
async def test_webhook_http200_with_business_error_is_failed(monkeypatch):
    """HTTP 200 但平台业务码非 0（钉钉/企微 errcode）：业务失败，不得记成功。"""
    _fake_httpx(
        monkeypatch,
        response=_FakeResponse(200, {"errcode": 301001, "errmsg": "keywords not match"}),
    )
    result = await execute_webhook_notify({
        "webhook_url": "https://example.com/hook", "message": "m", "webhook_type": "dingtalk",
    })
    assert result.status == STATUS_FAILED
    assert "errcode=301001" in (result.error or "")


# ════════════════════════════════════════════════════════════════════════════
# 验收 2：HTTP 500 不得被无条件记录为成功
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_500_not_recorded_as_success(monkeypatch):
    _fake_httpx(monkeypatch, response=_FakeResponse(500, text="boom"))
    # 副作用请求（POST）：5xx 无法确认是否生效 -> unknown，绝不 succeeded
    post = await execute_http_post({"url": "https://api.example.com/orders", "body": {}})
    assert post.status == STATUS_UNKNOWN
    assert post.outcome_known is False
    # 只读请求（GET）：5xx 明确失败 -> failed（可重试）
    get = await execute_http_get({"url": "https://api.example.com/data"})
    assert get.status == STATUS_FAILED
    assert get.retryable is True


@pytest.mark.asyncio
async def test_http_post_500_ledger_records_unknown(monkeypatch):
    """账本层：HTTP 500 的 POST 记 unknown，不得伪装成功。"""
    _stub_public_dns(monkeypatch)
    _fake_httpx(monkeypatch, response=_FakeResponse(500, text="boom"))
    run_id, v1 = await _claimed_run()
    bus = _RecordingBus()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=v1), ["http_post"], bus)

    await adapter._execute("http_post", {
        "url": "https://api.example.com/orders", "body": {"a": 1}, "tool_call_id": "tc-500",
    })

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-500")
    assert rec.status == TOOL_EXEC_UNKNOWN, "HTTP 500 不得被记录为成功"
    assert TOOL_COMPLETED not in bus.published


# ════════════════════════════════════════════════════════════════════════════
# 验收 3：邮件服务器连接中断等不确定情况进入 unknown
# ════════════════════════════════════════════════════════════════════════════


def _email_params():
    return {
        "to": "user@example.com", "subject": "s", "body": "b",
        "smtp_host": "smtp.example.com", "smtp_port": 587,
        "smtp_user": "u", "smtp_password": "p",
    }


def _smtp_raising(monkeypatch, exc: Exception):
    class _SMTP:
        def __init__(self, *a, **k):
            raise exc

    monkeypatch.setattr(notify_tools.smtplib, "SMTP", _SMTP)


@pytest.mark.asyncio
async def test_mail_connection_interrupt_goes_unknown(monkeypatch):
    # 会话中断/超时/连接重置：无法确认是否已投递 -> unknown
    for exc in (
        smtplib.SMTPServerDisconnected("connection lost"),
        TimeoutError("smtp timeout"),
        ConnectionResetError("reset"),
    ):
        _smtp_raising(monkeypatch, exc)
        result = await execute_send_email_tool(_email_params())
        assert result.status == STATUS_UNKNOWN, f"{type(exc).__name__} 必须进入 unknown"
        assert result.outcome_known is False


@pytest.mark.asyncio
async def test_mail_definitive_failure_goes_failed(monkeypatch):
    # 确认未投递 -> failed（不是 unknown，也不是 succeeded）
    for exc in (
        smtplib.SMTPAuthenticationError(535, b"auth failed"),
        ConnectionRefusedError("refused"),
        smtplib.SMTPRecipientsRefused({}),
    ):
        _smtp_raising(monkeypatch, exc)
        result = await execute_send_email_tool(_email_params())
        assert result.status == STATUS_FAILED, f"{type(exc).__name__} 必须进入 failed"


@pytest.mark.asyncio
async def test_mail_data_error_by_smtp_code(monkeypatch):
    # DATA 阶段 5xx 明确未投递 -> failed；4xx 临时拒绝无法确认 -> unknown
    _smtp_raising(monkeypatch, smtplib.SMTPDataError(550, b"rejected"))
    assert (await execute_send_email_tool(_email_params())).status == STATUS_FAILED
    _smtp_raising(monkeypatch, smtplib.SMTPDataError(451, b"temporarily"))
    assert (await execute_send_email_tool(_email_params())).status == STATUS_UNKNOWN


@pytest.mark.asyncio
async def test_mail_missing_params_is_failed(monkeypatch):
    result = await execute_send_email_tool({"to": "", "subject": "s", "body": "b"})
    assert result.status == STATUS_FAILED


# ════════════════════════════════════════════════════════════════════════════
# 验收 4：Webhook 超时不会被当作成功
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_webhook_timeout_not_success(monkeypatch):
    _fake_httpx(monkeypatch, exc=httpx.TimeoutException("webhook timeout"))
    result = await execute_webhook_notify({
        "webhook_url": "https://example.com/hook", "message": "m",
    })
    assert result.status == STATUS_UNKNOWN, "Webhook 超时不得当作成功"
    assert result.outcome_known is False


@pytest.mark.asyncio
async def test_webhook_status_code_semantics(monkeypatch):
    params = {"webhook_url": "https://example.com/hook", "message": "m"}
    # 5xx -> unknown（无法确认是否生效）
    _fake_httpx(monkeypatch, response=_FakeResponse(502, text="bad gateway"))
    assert (await execute_webhook_notify(params)).status == STATUS_UNKNOWN
    # 4xx -> failed（远端明确拒绝）
    _fake_httpx(monkeypatch, response=_FakeResponse(400, text="bad request"))
    assert (await execute_webhook_notify(params)).status == STATUS_FAILED
    # 2xx + 干净业务码 -> succeeded
    _fake_httpx(monkeypatch, response=_FakeResponse(200, {"errcode": 0, "errmsg": "ok"}))
    assert (await execute_webhook_notify(params)).status == STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_webhook_timeout_ledger_records_unknown(monkeypatch):
    _fake_httpx(monkeypatch, exc=httpx.TimeoutException("webhook timeout"))
    run_id, v1 = await _claimed_run()
    bus = _RecordingBus()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=v1), ["webhook_notify"], bus)

    await adapter._execute("webhook_notify", {
        "webhook_url": "https://example.com/hook", "message": "m", "tool_call_id": "tc-to",
    })

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-to")
    assert rec.status == TOOL_EXEC_UNKNOWN
    assert TOOL_COMPLETED not in bus.published


# ════════════════════════════════════════════════════════════════════════════
# 验收 5：所有工具均经过统一结果归一化
# ════════════════════════════════════════════════════════════════════════════


def test_normalize_unifies_all_handler_returns():
    # str -> succeeded（业务错误必须显式 ToolResult.failed，禁止伪装成功字符串）
    r = normalize("plain output")
    assert r.status == STATUS_SUCCEEDED and r.text == "plain output"
    # None -> 无输出成功
    r = normalize(None)
    assert r.status == STATUS_SUCCEEDED
    # 任意值 -> succeeded
    r = normalize({"k": 1})
    assert r.status == STATUS_SUCCEEDED and json.loads(r.text) == {"k": 1}
    # ToolResult 原样透传（不被二次包装/改判）
    for tr in (
        ToolResult.succeeded("ok"),
        ToolResult.failed("err"),
        ToolResult.unknown("unknown outcome"),
    ):
        assert normalize(tr) is tr


@pytest.mark.asyncio
async def test_all_tool_executor_returns_are_toolresult():
    """ToolExecutor 边界：任何工具返回值都是 ToolResult（含未知工具/异常路径）。"""
    executor = ToolExecutor(None, {})
    # 未知工具 -> failed（不是异常、不是伪成功字符串）
    r = await executor.execute("no_such_tool", {})
    assert isinstance(r, ToolResult) and r.status == STATUS_FAILED
    # 真实 handler（同步计算器）str 返回归一化为 ToolResult
    r = await executor.execute("calculator", {"expression": "1+1"})
    assert isinstance(r, ToolResult) and r.status == STATUS_SUCCEEDED
    # 归一化异常路径（raise_on_error=False）
    r = await executor.execute("http_get", {"url": "not-a-valid-url"})
    assert isinstance(r, ToolResult) and r.status in (STATUS_FAILED, STATUS_UNKNOWN)


@pytest.mark.asyncio
async def test_adapter_normalizes_str_result_to_succeeded_ledger(monkeypatch):
    """str 结果经统一归一化 -> 账本 succeeded（归一化链路端到端）。"""
    run_id, v1 = await _claimed_run()
    bus = _RecordingBus()
    adapter = _make_adapter(_make_ctx(run_id, lease_version=v1), ["calculator"], bus)

    async def _exec(self, name, args, *, raise_on_error=False):
        return "42"  # 旧式 str 返回

    monkeypatch.setattr(ToolExecutor, "execute", _exec)
    text = await adapter._execute("calculator", {"expression": "6*7", "tool_call_id": "tc-str"})
    assert text == "42"

    async with AsyncSessionLocal() as s:
        rec = await ToolExecutionRepository(s).get(run_id, "tc-str")
    assert rec.status == TOOL_EXEC_SUCCEEDED
    assert rec.result_payload == "42"
    assert TOOL_COMPLETED in bus.published


# ════════════════════════════════════════════════════════════════════════════
# 验收 6：长时间 SMTP 调用不会阻塞 Worker Heartbeat
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_smtp_does_not_block_event_loop(monkeypatch):
    """同步 smtplib 必须在工作线程执行：事件循环持续调度（Heartbeat 依赖它）。"""
    smtp_threads = []

    class _SlowSMTP:
        def __init__(self, host, port, timeout=None):
            smtp_threads.append(threading.get_ident())
            time.sleep(0.25)  # 同步阻塞调用：若在事件循环线程会卡死 Heartbeat
            # 模拟已建立的连接（对端为公网地址）：连接阶段对端 IP 校验依赖 sock
            class _Sock:
                @staticmethod
                def getpeername():
                    return ("8.8.8.8", 587)
            self.sock = _Sock()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, *a):
            pass

    monkeypatch.setattr(notify_tools.smtplib, "SMTP", _SlowSMTP)

    ticks = []

    async def _ticker(stop_at: float):
        while time.monotonic() < stop_at:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    stop_at = time.monotonic() + 0.4
    task = asyncio.create_task(execute_send_email_tool(_email_params()))
    await _ticker(stop_at)
    result = await task

    assert result.status == STATUS_SUCCEEDED
    # SMTP 全流程在非事件循环线程执行
    assert smtp_threads and smtp_threads[0] != threading.get_ident()
    # 事件循环未被阻塞：tick 间隔均匀（若被 0.25s 阻塞会出现大间隔）
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "ticker 未运行"
    assert max(gaps) < 0.15, f"SMTP 同步调用阻塞了事件循环（Heartbeat）: max gap={max(gaps):.3f}s"

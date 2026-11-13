"""工具安全基线验证（P1-07 / 任务书 §15）。

SEC-01 webhook_notify 访问内网/回环/云元数据地址被拦截（SSRF 统一入口）
SEC-02 SMTP Host 受统一网络策略限制（显式 allowlist 可受控放行）
SEC-03 文件工具符号链接逃逸被拦截
SEC-04 db_query SQL 绕过测试（CTE / 注释 / 多语句）全部拦截
SEC-05 smtp_pass、Authorization、Webhook Token 不进入事件日志和工具账本
SEC-06 未启用外部 Sandbox 时生产模式不暴露不安全 Python 执行
SEC-07 allowlist 前缀匹配误放行修复（trusted.example.evil 拒绝）
SEC-08 未登记风险分类的工具默认拒绝（不得默认 READ）
"""
import asyncio
import os

import pytest
import pytest_asyncio

from app.core.database import init_db

from app.services.agent_runtime.events import redact_secrets, redact_sensitive
from app.services.agent_runtime.tools.adapter import ToolAdapterFactory
from app.services.agent_runtime.tools.policy import (
    ToolPolicy,
    ToolPolicyError,
    ToolRisk,
    ToolRiskProfile,
    get_tool_risk,
    register_tool_risk,
    unregister_tool_risk,
)

from test_agent_runtime import make_db_ctx


@pytest_asyncio.fixture(autouse=True)
async def _db_schema():
    await init_db()
    yield


def _bus():
    from app.services.agent_runtime.events import EventBus

    return EventBus()


# ── SEC-01：webhook_notify SSRF（内网 / 回环 / 云元数据）────────────────────

class TestWebhookSSRF:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/hook",
        "http://localhost/hook",
        "http://10.1.2.3/hook",
        "http://172.16.0.9/hook",
        "http://192.168.1.10/hook",
        "http://169.254.169.254/latest/meta-data/",
    ])
    async def test_sec01_webhook_internal_targets_rejected(self, url):
        """webhook_notify 访问内网、回环和云元数据地址会被拦截。"""
        ctx = await make_db_ctx()
        factory = ToolAdapterFactory(
            ctx, ToolPolicy(allowed_tools={"webhook_notify"}), _bus(),
        )
        with pytest.raises(ToolPolicyError):
            await factory._execute("webhook_notify", {"webhook_url": url, "message": "x"})

    def test_sec01_policy_rejects_metadata_host_by_name(self):
        p = ToolPolicy(allowed_tools={"webhook_notify"})
        with pytest.raises(ToolPolicyError):
            p.check_url("http://metadata.google.internal/computeMetadata/v1/")


# ── SEC-02：SMTP Host 统一网络策略 ─────────────────────────────────────────

class TestSMTPUnifiedPolicy:
    @pytest.mark.asyncio
    async def test_sec02_smtp_internal_host_rejected(self):
        """send_email_tool 的 smtp_host 走同一 URL Policy：内网目标拦截。"""
        ctx = await make_db_ctx()
        factory = ToolAdapterFactory(
            ctx, ToolPolicy(allowed_tools={"send_email_tool"}), _bus(),
        )
        with pytest.raises(ToolPolicyError):
            await factory._execute("send_email_tool", {
                "smtp_host": "10.1.2.3", "smtp_port": 587,
                "to": "a@example.com", "subject": "s", "body": "b",
            })

    def test_sec02_smtp_allowlist_explicit_trust(self):
        """显式 allowlist 是受控放行通道（策略层面，不发起真实连接）。"""
        p = ToolPolicy(
            allowed_tools={"send_email_tool"}, network_allowlist=["smtp.corp.example"],
        )
        p.check_url("http://smtp.corp.example:587")  # 不抛异常即受控放行

    def test_sec02_smtp_peer_ip_guard_blocks_loopback(self):
        """SMTP 连接阶段对端 IP 校验（DNS Rebinding 等价防护）。"""
        from app.services.tools.notify_tools import _verify_smtp_peer

        class _FakeServer:
            class sock:
                @staticmethod
                def getpeername():
                    return ("127.0.0.1", 25)

        with pytest.raises(ConnectionRefusedError):
            _verify_smtp_peer(_FakeServer(), "smtp.evil.example")

    def test_sec02_smtp_peer_ip_guard_allows_allowlisted_host(self, monkeypatch):
        from app.services.tools.notify_tools import _verify_smtp_peer

        class _FakeServer:
            class sock:
                @staticmethod
                def getpeername():
                    return ("127.0.0.1", 25)

        import app.core.config as core_config
        monkeypatch.setattr(core_config.settings, "AGENT_NETWORK_ALLOWLIST", "smtp.corp.example")
        # 显式信任的内网中继放行（与 URL Policy allowlist 语义一致）
        _verify_smtp_peer(_FakeServer(), "smtp.corp.example")


# ── SEC-03：文件工具符号链接逃逸 ──────────────────────────────────────────

class TestPathSymlinkEscape:
    def test_sec03_symlink_file_escape_rejected(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("top secret", encoding="utf-8")
        os.symlink(str(outside), str(root / "link.txt"))

        p = ToolPolicy(allowed_tools={"read_file"}, workspace_root=str(root))
        with pytest.raises(ToolPolicyError):
            p.check_path("link.txt")

    def test_sec03_symlink_dir_escape_rejected(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("top secret", encoding="utf-8")
        os.symlink(str(outside_dir), str(root / "sub"), target_is_directory=True)

        p = ToolPolicy(allowed_tools={"read_file"}, workspace_root=str(root))
        with pytest.raises(ToolPolicyError):
            p.check_path("sub/secret.txt")

    def test_sec03_normal_path_allowed(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "ok.txt").write_text("ok", encoding="utf-8")
        p = ToolPolicy(allowed_tools={"read_file"}, workspace_root=str(root))
        p.check_path("ok.txt")  # 不抛异常即允许
        p.check_path("new_file.txt")  # 尚不存在的新文件路径同样允许


# ── SEC-04：SQL 只读绕过（CTE / 注释 / 多语句）────────────────────────────

class TestSQLReadonlyBypass:
    def test_sec04_cte_write_bypass_rejected(self):
        p = ToolPolicy(allowed_tools={"db_query"})
        for sql in [
            "WITH x AS (SELECT 1) UPDATE t SET a=1",
            "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
            "WITH x AS (SELECT 1) DELETE FROM t",
            "WITH x AS (SELECT 1), y AS (DROP TABLE t) SELECT 1",
        ]:
            with pytest.raises(ToolPolicyError):
                p.check_sql_readonly(sql)

    def test_sec04_comment_and_multistatement_bypass_rejected(self):
        p = ToolPolicy(allowed_tools={"db_query"})
        for sql in [
            "SELECT 1; DROP TABLE t",
            "SELECT 1 -- ok\n; DROP TABLE t",
            "SELECT 1 /* c */; DELETE FROM t",
            "SELECT 1 # mysql comment\n; TRUNCATE t",
            "/* comment */ DELETE FROM t",
            "-- only comment\nUPDATE t SET a=1",
            "SELECT 1;;",
        ]:
            with pytest.raises(ToolPolicyError):
                p.check_sql_readonly(sql)

    def test_sec04_legit_readonly_allowed(self):
        p = ToolPolicy(allowed_tools={"db_query"})
        for sql in [
            "SELECT 1",
            "select * from updates",                       # 表名 updates 不是 UPDATE 语句
            "SELECT updated_at FROM t",                    # 列名 updated_at 不误伤
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "/* c */ SELECT 1",
            "SELECT 1 -- hi",
            "SELECT 'drop table t' AS s",                  # 字符串字面量不误伤
            "SELECT replace(a, 'x', 'y') FROM t",          # replace() 函数不误伤
            "EXPLAIN SELECT 1",
            "SHOW TABLES",
            "SELECT 1;",                                   # 单语句末尾分号允许
        ]:
            p.check_sql_readonly(sql)


# ── SEC-05：敏感凭据不进事件日志与工具账本 ────────────────────────────────

class TestSecretRedaction:
    def test_sec05_redact_masks_secrets(self):
        out = redact_sensitive({
            "smtp_pass": "P@ssw0rd-SECRET",
            "smtp_password": "P@ssw0rd-SECRET",
            "headers": {"Authorization": "Bearer sk-ABCDEF"},
            "webhook_url": "https://hook.example/x?access_token=WEBHOOKTOKEN",
            "note": "plain",
        })
        assert out["smtp_pass"] == "***" and out["smtp_password"] == "***"
        assert out["headers"]["Authorization"] == "***"
        assert "WEBHOOKTOKEN" not in out["webhook_url"]
        assert out["note"] == "plain"
        text = redact_secrets(
            "mail error: Authorization: Bearer sk-ABCDEF "
            "relay=smtp://user:SECRETPASS@smtp.example:587"
        )
        assert "sk-ABCDEF" not in text and "SECRETPASS" not in text

    @pytest.mark.asyncio
    async def test_sec05_secrets_not_in_ledger_or_events(self, monkeypatch):
        """smtp_pass / Webhook Token 不进入事件日志和工具账本（真实执行）。"""
        # 本用例验证脱敏边界：放行网络策略检查，真实执行 SMTP（连接被拒 -> failed）
        monkeypatch.setattr(ToolPolicy, "check_url", lambda self, u: None)
        ctx = await make_db_ctx()
        bus = _bus()
        factory = ToolAdapterFactory(
            ctx, ToolPolicy(allowed_tools={"send_email_tool"}), bus,
        )
        secret = "P@ssw0rd-SEC05SECRET"
        await factory._execute("send_email_tool", {
            "tool_call_id": "sec05-c1",
            "smtp_host": "127.0.0.1", "smtp_port": 1,
            "smtp_user": "u@example.com", "smtp_password": secret,
            "to": "a@example.com", "subject": "s", "body": "b",
        })

        from app.core.database import AsyncSessionLocal
        from app.services.agent_runtime.repositories import ToolExecutionRepository

        async with AsyncSessionLocal() as s:
            rec = await ToolExecutionRepository(s).get(ctx.run_id, "sec05-c1")
        assert rec is not None
        dumped = f"{rec.args_snapshot}|{rec.result_payload}|{rec.error}"
        assert secret not in dumped, "smtp_pass 泄漏进工具账本"
        assert rec.args_snapshot.get("smtp_password") == "***"

        while not bus._queue.empty():
            ev = bus._queue.get_nowait()
            assert secret not in str(ev.data), "凭据泄漏进事件日志"


# ── SEC-06：无外部 Sandbox 不暴露不安全 Python 执行 ───────────────────────

class TestPythonExecSandbox:
    def test_sec06_restricted_mode_denies_python_exec(self):
        """restricted（进程内 AST/线程限制）不是安全沙箱，默认拒绝。"""
        p = ToolPolicy(allowed_tools={"python_exec"}, sandbox_mode="restricted")
        with pytest.raises(ToolPolicyError):
            p.check_exec_allowed("python_exec")

    def test_sec06_external_sandbox_allows(self):
        p = ToolPolicy(allowed_tools={"python_exec"}, sandbox_mode="external")
        p.check_exec_allowed("python_exec")  # 接入外部 Sandbox 后放行

    def test_sec06_dev_escape_hatch(self):
        p = ToolPolicy(
            allowed_tools={"python_exec"}, sandbox_mode="restricted",
            allow_unsafe_python=True,
        )
        p.check_exec_allowed("python_exec")  # 仅限开发环境显式开关

    def test_sec06_production_from_settings_denies(self, monkeypatch):
        """生产模式即使配置了逃生开关也强制禁用（开关仅限开发）。"""
        from app.core.config import settings

        monkeypatch.setattr(settings, "APP_ENV", "production")
        monkeypatch.setattr(settings, "AGENT_SANDBOX_MODE", "restricted")
        monkeypatch.setattr(settings, "AGENT_ALLOW_UNSAFE_PYTHON", True)
        p = ToolPolicy.from_settings({"python_exec"})
        assert p.allow_unsafe_python is False
        with pytest.raises(ToolPolicyError):
            p.check_exec_allowed("python_exec")

    def test_sec06_non_exec_tools_unaffected(self):
        p = ToolPolicy(allowed_tools={"calculator"}, sandbox_mode="restricted")
        p.check_exec_allowed("calculator")


# ── SEC-07：allowlist 前缀误放行修复 ──────────────────────────────────────

class TestAllowlistPrefix:
    def test_sec07_prefix_match_rejected(self):
        p = ToolPolicy(allowed_tools={"http_get"}, network_allowlist=["trusted.example"])
        with pytest.raises(ToolPolicyError):
            p.check_url("https://trusted.example.evil.com/steal")

    def test_sec07_exact_and_subdomain_allowed(self):
        p = ToolPolicy(allowed_tools={"http_get"}, network_allowlist=["trusted.example"])
        p.check_url("https://trusted.example/")
        p.check_url("https://api.trusted.example/v1")


# ── SEC-08：未登记工具默认拒绝 ────────────────────────────────────────────

class TestUnregisteredToolDenied:
    def test_sec08_unregistered_tool_denied(self):
        p = ToolPolicy(allowed_tools={"mystery_tool"})
        with pytest.raises(ToolPolicyError):
            p.check_tool_allowed("mystery_tool")
        with pytest.raises(ToolPolicyError):
            p.check_exec_allowed("mystery_tool")
        # 未登记工具不得默认 READ：默认要求人工审批（且执行已被拒绝）
        assert p.requires_approval("mystery_tool") is True

    def test_sec08_no_default_read_risk(self):
        assert get_tool_risk("mystery_tool") is None

    def test_sec08_registration_makes_tool_usable(self):
        register_tool_risk(ToolRiskProfile(
            name="custom_calc_tool", risk=ToolRisk.READ,
            side_effect_class="read_only", requires_approval=False,
            retry_policy="safe_retry",
        ))
        try:
            p = ToolPolicy(allowed_tools={"custom_calc_tool"})
            p.check_tool_allowed("custom_calc_tool")
            p.check_exec_allowed("custom_calc_tool")
            assert p.requires_approval("custom_calc_tool") is False
        finally:
            unregister_tool_risk("custom_calc_tool")

    def test_sec08_builtin_profiles_declare_full_risk_tuple(self):
        """每个登记工具都显式声明 side-effect class / risk / approval / retry。"""
        for name in ("python_exec", "http_post", "webhook_notify", "send_email_tool",
                     "delete_file", "http_get", "calculator", "db_query"):
            profile = get_tool_risk(name)
            assert profile is not None, name
            assert profile.risk is not None
            assert profile.side_effect_class in ("read_only", "external_idempotent", "non_idempotent")
            assert isinstance(profile.requires_approval, bool)
            assert profile.retry_policy in ("safe_retry", "idempotent_key", "none")

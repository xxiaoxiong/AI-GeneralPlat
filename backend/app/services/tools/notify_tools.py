"""
通知工具 - 发送邮件 / Webhook 通知

send_email_tool   📧 发送邮件（SMTP/SSL）
webhook_notify    🔔 发送 Webhook 通知（钉钉/企微/飞书/自定义）

P0-05（任务书 §7）改造：
- 统一返回 ToolResult，错误不得伪装为成功字符串（§7.2 C）；
- SMTP 确认未送达 -> failed；超时/传输中断 -> unknown（无法确认是否已投递）；
- Webhook 检查 HTTP 状态 + 平台业务码（errcode!=0 即使 HTTP 200 也是业务失败）；
- 同步 smtplib 全流程放入 asyncio.to_thread，不阻塞事件循环 / Worker Heartbeat（§7.2 D）。
"""
import asyncio
import ipaddress
import os
import smtplib
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from app.core.config import settings

from .result import ToolResult


# 确认未产生投递副作用 -> failed（§7.2 B）
_SMTP_DEFINITIVE_ERRORS = (
    smtplib.SMTPAuthenticationError,   # 认证失败：未投递
    smtplib.SMTPRecipientsRefused,     # 所有收件人被拒：未投递
    smtplib.SMTPSenderRefused,         # 发件人被拒：未投递
    smtplib.SMTPHeloError,             # 握手被拒：未投递
    smtplib.SMTPNotSupportedError,     # 服务不支持（如 STARTTLS）：未投递
    ConnectionRefusedError,            # TCP 连接被拒：未建立连接
    socket.gaierror,                   # DNS 解析失败：未建立连接
)

# 传输中断/超时：无法确认是否已投递 -> unknown（禁止无条件重试）
_SMTP_UNCERTAIN_ERRORS = (
    TimeoutError,                      # 含 socket.timeout：可能已投递
    smtplib.SMTPServerDisconnected,    # 会话中途断开
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


def _verify_smtp_peer(server: smtplib.SMTP, host: str) -> None:
    """SMTP 连接阶段对端 IP 校验（§15.2 B：DNS Rebinding 防护的等价机制）。

    执行前 URL Policy 只能看到解析前的主机名；连接建立后在 EHLO 之前校验
    实际对端 IP，防止 DNS 解析后被重绑定到内网/回环/链路本地地址。
    受 AGENT_NETWORK_ALLOWLIST 显式信任的主机跳过（与 URL Policy allowlist
    语义一致：显式信任决策可含内网中继）。
    """
    allow = [x.strip() for x in (getattr(settings, "AGENT_NETWORK_ALLOWLIST", "") or "").split(",") if x.strip()]
    if any(host == a or host.endswith("." + a) for a in allow):
        return
    try:
        peer = server.sock.getpeername()[0]
    except OSError as e:
        raise ConnectionRefusedError(f"SMTP 连接对端无法确认，拒绝继续: {e}") from e
    ip = ipaddress.ip_address(peer)
    if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved:
        raise ConnectionRefusedError(
            f"SMTP 对端 {peer} 为内网/回环/链路本地地址，已按 SSRF 策略拒绝（host={host}）"
        )


async def execute_send_email_tool(params: dict) -> ToolResult:
    """
    发送邮件（SMTP over SSL/TLS）

    参数（也可从环境变量读取）：
      smtp_host, smtp_port, smtp_user, smtp_password, to, subject, body, html

    结果语义（§7.2 B/C）：
      - 参数缺失 / 认证失败 / 连接被拒 / 收件人被拒 -> failed（确认未送达）；
      - 超时 / 连接中断 / 会话断开 -> unknown（无法确认是否已投递）；
      - 远端确认接收 -> succeeded。

    依赖：pip install secure-smtplib（标准库 smtplib）
    """
    smtp_host = params.get("smtp_host") or os.getenv("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(params.get("smtp_port") or os.getenv("SMTP_PORT", 587))
    smtp_user = params.get("smtp_user") or os.getenv("SMTP_USER")
    smtp_pass = params.get("smtp_password") or os.getenv("SMTP_PASSWORD")
    to        = params.get("to")
    subject   = params.get("subject", "")
    body      = params.get("body", "")
    html      = params.get("html")

    if not all([smtp_user, smtp_pass, to]):
        return ToolResult.failed("错误：缺少必要参数（smtp_user, smtp_password, to）")

    # 构建邮件
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to

    def _smtp_send() -> None:
        # 同步 smtplib 全流程在受控线程池执行（§7.2 D）：
        # 不阻塞事件循环，Worker Heartbeat 得以继续；超时由 smtplib timeout 兜底。
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            # §15.2 B：连接建立后 EHLO 前校验对端 IP（DNS Rebinding 等价防护）
            _verify_smtp_peer(server, smtp_host)
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to.split(","), msg.as_string())

    try:
        await asyncio.to_thread(_smtp_send)
    except smtplib.SMTPDataError as e:
        # DATA 阶段被拒：5xx 明确未投递，4xx 临时拒绝无法确认
        if getattr(e, "smtp_code", 0) >= 500:
            return ToolResult.failed(f"邮件发送失败: {e}")
        return ToolResult.unknown(f"邮件发送结果未知（服务端临时拒绝）: {e}")
    except _SMTP_DEFINITIVE_ERRORS as e:
        return ToolResult.failed(f"邮件发送失败: {e}")
    except _SMTP_UNCERTAIN_ERRORS as e:
        return ToolResult.unknown(f"邮件发送结果未知（连接中断/超时）: {e}")
    return ToolResult.succeeded(f"邮件已成功发送至: {to}")


async def execute_webhook_notify(params: dict) -> ToolResult:
    """
    通用 Webhook 通知
    支持钉钉、企业微信、飞书、自定义 Webhook

    参数：
      webhook_url   Webhook 地址（必填）
      message       消息内容（必填）
      msg_type      消息类型：text/markdown（默认 text）
      at_users      @用户列表（钉钉/企微手机号或 open_id）
      at_all        是否@所有人（默认 False）
      webhook_type  Webhook 类型：dingtalk/wecom/feishu/custom（默认 custom）

    结果语义（§7.2 B/C）：
      - 缺参 -> failed；
      - 超时 / 传输中断 -> unknown（通知可能已发出，既不得记成功也不得盲目重发）；
      - HTTP 4xx -> failed；HTTP 5xx -> unknown（无法确认是否生效）；
      - HTTP 2xx 但平台业务码（钉钉/企微 errcode、飞书 code）非 0 -> failed
        （业务失败即使 HTTP 200 也不得记成功）；
      - HTTP 2xx 且业务码正常 -> succeeded。
    """
    import httpx

    webhook_url  = params.get("webhook_url")
    message      = params.get("message")
    msg_type     = params.get("msg_type", "text")
    at_users     = params.get("at_users", [])
    at_all       = params.get("at_all", False)
    webhook_type = params.get("webhook_type", "custom")

    if not webhook_url or not message:
        return ToolResult.failed("错误：webhook_url 和 message 为必填项")

    # 构建 payload
    if webhook_type == "dingtalk":
        payload = {
            "msgtype": msg_type,
            "text" if msg_type == "text" else "markdown": {"content": message},
        }
        if at_users or at_all:
            payload["at"] = {"atMobiles": at_users, "isAtAll": at_all}
    elif webhook_type == "wecom":
        payload = {"msgtype": msg_type, msg_type: {"content": message}}
        if at_users:
            payload[msg_type]["mentioned_list"] = at_users
    elif webhook_type == "feishu":
        payload = {
            "msg_type": "text" if msg_type == "text" else "post",
            "content": {"text": message} if msg_type == "text" else {"content": [[{"tag": "text", "text": message}]]},
        }
    else:  # custom
        payload = {"message": message, "msg_type": msg_type}
        if at_users:
            payload["at_users"] = at_users

    try:
        # AGENT_EGRESS_PROXY 非空时经受控 Egress Proxy 出口（§15.2 B）
        async with httpx.AsyncClient(
            timeout=10, proxy=(getattr(settings, "AGENT_EGRESS_PROXY", "") or None),
        ) as client:
            response = await client.post(webhook_url, json=payload)
    except httpx.TimeoutException as e:
        return ToolResult.unknown(f"Webhook 发送结果未知（超时）: {e}")
    except httpx.HTTPError as e:
        # 传输中断/连接失败：无法确认通知是否已发出
        return ToolResult.unknown(f"Webhook 发送结果未知（传输中断）: {e}")

    resp_text = response.text[:200]
    if response.status_code >= 500:
        return ToolResult.unknown(
            f"Webhook 发送结果未知（HTTP {response.status_code}）: {resp_text}"
        )
    if not response.is_success:
        return ToolResult.failed(
            f"Webhook 发送失败（HTTP {response.status_code}）: {resp_text}"
        )
    # HTTP 2xx 不等于业务成功：继续核对平台业务码
    biz_err = _webhook_business_error(response, webhook_type)
    if biz_err is not None:
        return ToolResult.failed(f"Webhook 发送失败（业务错误，HTTP {response.status_code}）: {biz_err}")
    return ToolResult.succeeded(f"Webhook 通知已发送（HTTP {response.status_code}）: {resp_text}")


def _webhook_business_error(response, webhook_type: str) -> Optional[str]:
    """按平台契约判定业务错误：钉钉/企微 errcode、飞书 code，非 0 即业务失败。

    自定义 Webhook 无平台业务码契约，仅按 HTTP 状态判定（§7.2 B 按契约判断）。
    """
    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    errcode = data.get("errcode")
    if isinstance(errcode, int) and errcode != 0:
        return f"errcode={errcode} errmsg={data.get('errmsg', '')}"
    if webhook_type == "feishu":
        code = data.get("code")
        if isinstance(code, int) and code != 0:
            return f"code={code} msg={data.get('msg', '')}"
    return None


# ── 工具注册表 ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "send_email_tool",
        "display_name": "邮件发送",
        "category": "notification",
        "description": "发送邮件（支持 HTML）",
        "parameters": {
            "to": {"type": "string", "description": "收件人邮箱，多个用逗号分隔"},
            "subject": {"type": "string", "description": "邮件主题"},
            "body": {"type": "string", "description": "邮件正文（纯文本）"},
            "html": {"type": "string", "description": "HTML 正文（可选）"},
            "smtp_host": {"type": "string", "description": "SMTP 服务器（可选）"},
            "smtp_port": {"type": "integer", "description": "SMTP 端口（可选）"},
            "smtp_user": {"type": "string", "description": "SMTP 用户名（可选）"},
            "smtp_password": {"type": "string", "description": "SMTP 密码（可选）"},
        },
        "required": ["to", "subject", "body"],
    },
    {
        "name": "webhook_notify",
        "display_name": "Webhook 通知",
        "category": "notification",
        "description": "发送 Webhook 通知（钉钉/企微/飞书/自定义）",
        "parameters": {
            "webhook_url": {"type": "string", "description": "Webhook URL"},
            "message": {"type": "string", "description": "消息内容"},
            "msg_type": {"type": "string", "description": "消息类型 text/markdown"},
            "at_users": {"type": "array", "description": "@用户列表"},
            "at_all": {"type": "boolean", "description": "是否@所有人"},
            "webhook_type": {"type": "string", "description": "dingtalk/wecom/feishu/custom"},
        },
        "required": ["webhook_url", "message"],
    },
]

HANDLERS = {
    "send_email_tool": execute_send_email_tool,
    "webhook_notify": execute_webhook_notify,
}

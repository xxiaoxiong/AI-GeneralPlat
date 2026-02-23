"""📧 通知推送工具：send_email_tool, webhook_notify"""
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, List

import httpx


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "send_email_tool",
        "display_name": "📧 发送邮件",
        "category": "通知推送",
        "description": "通过 SMTP 发送邮件通知，支持 HTML 正文",
        "parameters": {
            "to":        {"type": "string", "description": "收件人邮箱，多个用逗号分隔"},
            "subject":   {"type": "string", "description": "邮件主题"},
            "body":      {"type": "string", "description": "邮件正文（支持 HTML）"},
            "smtp_host": {"type": "string", "description": "SMTP 服务器，如 smtp.qq.com", "default": "smtp.qq.com"},
            "smtp_port": {"type": "integer","description": "SMTP 端口，默认587", "default": 587},
            "smtp_user": {"type": "string", "description": "发件人账号"},
            "smtp_pass": {"type": "string", "description": "发件人密码/授权码"},
        },
        "required": ["to", "subject", "body", "smtp_user", "smtp_pass"],
    },
    {
        "name": "webhook_notify",
        "display_name": "📧 Webhook 通知",
        "category": "通知推送",
        "description": "向指定 Webhook URL 发送通知，支持企业微信、钉钉、飞书、自定义",
        "parameters": {
            "webhook_url": {"type": "string", "description": "Webhook URL"},
            "message":     {"type": "string", "description": "通知内容"},
            "type":        {"type": "string", "description": "类型: wecom(企业微信)/dingtalk(钉钉)/feishu(飞书)/custom(自定义)", "default": "custom"},
            "title":       {"type": "string", "description": "消息标题（部分平台支持）", "default": "AI Agent 通知"},
        },
        "required": ["webhook_url", "message"],
    },
]


async def execute_send_email_tool(params: dict) -> str:
    to        = params.get("to", "")
    subject   = params.get("subject", "")
    body      = params.get("body", "")
    smtp_host = params.get("smtp_host", "smtp.qq.com") or "smtp.qq.com"
    smtp_port = int(params.get("smtp_port", 587))
    smtp_user = params.get("smtp_user", "")
    smtp_pass = params.get("smtp_pass", "")

    if not all([to, subject, body, smtp_user, smtp_pass]):
        return "错误：缺少必要参数（to/subject/body/smtp_user/smtp_pass）"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = to
        subtype = "html" if "<" in body and ">" in body else "plain"
        msg.attach(MIMEText(body, subtype, "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [addr.strip() for addr in to.split(",")], msg.as_string())

        return f"邮件已成功发送至: {to}"
    except Exception as e:
        return f"邮件发送失败: {e}"


async def execute_webhook_notify(params: dict) -> str:
    webhook_url = params.get("webhook_url", "")
    message     = params.get("message", "")
    notify_type = params.get("type", "custom")
    title       = params.get("title", "AI Agent 通知")

    if not webhook_url or not message:
        return "错误：webhook_url 和 message 为必填项"

    # 按平台构造 payload
    if notify_type == "wecom":
        payload = {"msgtype": "text", "text": {"content": f"{title}\n{message}"}}
    elif notify_type == "dingtalk":
        payload = {
            "msgtype": "text",
            "text": {"content": f"{title}\n{message}"},
            "at": {"isAtAll": False},
        }
    elif notify_type == "feishu":
        payload = {
            "msg_type": "text",
            "content": {"text": f"{title}\n{message}"},
        }
    else:
        payload = {"title": title, "message": message}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
        return f"Webhook 通知已发送（HTTP {resp.status_code}）: {resp.text[:200]}"
    except Exception as e:
        return f"Webhook 发送失败: {e}"


HANDLERS = {
    "send_email_tool": execute_send_email_tool,
    "webhook_notify":  execute_webhook_notify,
}

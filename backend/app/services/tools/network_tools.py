"""🌐 网络请求工具：http_get, http_post"""
import json
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List

import httpx

from app.core.config import settings as _settings

from .result import ToolResult


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "http_get",
        "display_name": "🌐 HTTP GET",
        "category": "网络请求",
        "description": "发送 GET 请求获取数据，适合调用 REST API、获取网页内容等",
        "parameters": {
            "url":     {"type": "string",  "description": "请求 URL"},
            "headers": {"type": "object",  "description": "请求头，如 {'Authorization': 'Bearer xxx'}", "default": {}},
            "params":  {"type": "object",  "description": "URL 查询参数", "default": {}},
            "timeout": {"type": "integer", "description": "超时秒数，默认15", "default": 15},
        },
        "required": ["url"],
    },
    {
        "name": "http_post",
        "display_name": "🌐 HTTP POST",
        "category": "网络请求",
        "description": "发送 POST 请求提交数据，适合调用需要传参的 API",
        "parameters": {
            "url":     {"type": "string",  "description": "请求 URL"},
            "body":    {"type": "object",  "description": "请求体（JSON 格式）", "default": {}},
            "headers": {"type": "object",  "description": "请求头", "default": {}},
            "timeout": {"type": "integer", "description": "超时秒数，默认15", "default": 15},
        },
        "required": ["url"],
    },
]


async def execute_http_get(params: dict) -> ToolResult:
    # 请求异常不在此处吞掉：运行时工具账本需按异常语义区分 failed / unknown
    url = params.get("url", "")
    timeout = int(params.get("timeout", 15))
    headers = params.get("headers", {}) or {}
    query_params = params.get("params", {}) or {}
    async with httpx.AsyncClient(timeout=timeout, verify=(_settings.AGENT_CA_BUNDLE or _settings.AGENT_TLS_VERIFY)) as client:
        resp = await client.get(url, headers=headers, params=query_params)
    try:
        body = json.dumps(resp.json(), ensure_ascii=False, indent=2)
    except Exception:
        body = resp.text
    text = f"HTTP {resp.status_code} GET {url}\n{body[:3000]}"
    # 只读请求：非 2xx 是明确失败（可按策略重试，§7.2 B）
    if not resp.is_success:
        return ToolResult.failed(text, retryable=True)
    return ToolResult.succeeded(text)


async def execute_http_post(params: dict) -> ToolResult:
    # 请求异常不在此处吞掉：超时/连接中断可能发生在远端已成功之后，
    # 必须让调用方标记 unknown 而不是伪失败（F-07/F-08）
    url = params.get("url", "")
    timeout = int(params.get("timeout", 15))
    headers = params.get("headers", {}) or {}
    body = params.get("body", {}) or {}
    async with httpx.AsyncClient(timeout=timeout, verify=(_settings.AGENT_CA_BUNDLE or _settings.AGENT_TLS_VERIFY)) as client:
        resp = await client.post(url, json=body, headers=headers)
    try:
        resp_body = json.dumps(resp.json(), ensure_ascii=False, indent=2)
    except Exception:
        resp_body = resp.text
    text = f"HTTP {resp.status_code} POST {url}\n{resp_body[:3000]}"
    # 副作用请求不得把所有状态码当成功（§7.2 C）：
    # 2xx -> succeeded；4xx -> failed（远端明确拒绝）；5xx -> unknown（无法确认是否生效）
    if resp.is_success:
        return ToolResult.succeeded(text)
    if resp.status_code >= 500:
        return ToolResult.unknown(text)
    return ToolResult.failed(text)


HANDLERS = {
    "http_get":     execute_http_get,
    "http_request": execute_http_get,
    "http_post":    execute_http_post,
}

"""🌐 网络请求工具：http_get, http_post"""
import json
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List

import httpx


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


async def execute_http_get(params: dict) -> str:
    url = params.get("url", "")
    timeout = int(params.get("timeout", 15))
    headers = params.get("headers", {}) or {}
    query_params = params.get("params", {}) or {}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            resp = await client.get(url, headers=headers, params=query_params)
        try:
            body = json.dumps(resp.json(), ensure_ascii=False, indent=2)
        except Exception:
            body = resp.text
        return f"HTTP {resp.status_code} GET {url}\n{body[:3000]}"
    except Exception as e:
        return f"HTTP GET 请求失败: {e}"


async def execute_http_post(params: dict) -> str:
    url = params.get("url", "")
    timeout = int(params.get("timeout", 15))
    headers = params.get("headers", {}) or {}
    body = params.get("body", {}) or {}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            resp = await client.post(url, json=body, headers=headers)
        try:
            resp_body = json.dumps(resp.json(), ensure_ascii=False, indent=2)
        except Exception:
            resp_body = resp.text
        return f"HTTP {resp.status_code} POST {url}\n{resp_body[:3000]}"
    except Exception as e:
        return f"HTTP POST 请求失败: {e}"


HANDLERS = {
    "http_get":     execute_http_get,
    "http_request": execute_http_get,
    "http_post":    execute_http_post,
}

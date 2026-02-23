"""
🔎 网络增强工具：web_search, fetch_webpage, weather

这些工具提供 LLM 自身无法完成的能力：
  - web_search   : 实时搜索（多引擎：Bing/百度，无需 API Key）
  - fetch_webpage: 抓取网页正文内容
  - weather      : 实时天气查询（wttr.in，无需 API Key）

富媒体输出格式（LLM 在 Final Answer 中使用）：
  图片: ![描述](https://图片URL)
  链接: [链接文字](https://URL)
"""
import json
import re
from typing import Dict, Any, List

import httpx


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "web_search",
        "display_name": "🔎 网页搜索",
        "category": "网络增强",
        "description": (
            "实时搜索互联网获取最新信息（多引擎备用，无需 API Key）。"
            "返回标题、摘要和链接，LLM 可在回答中用 [标题](URL) 格式引用链接。"
        ),
        "parameters": {
            "query":       {"type": "string",  "description": "搜索关键词或问题"},
            "max_results": {"type": "integer", "description": "返回结果数量，默认5，最多10", "default": 5},
            "engine":      {"type": "string",  "description": "搜索引擎: auto/bing/baidu，默认auto", "default": "auto"},
        },
        "required": ["query"],
    },
    {
        "name": "fetch_webpage",
        "display_name": "🌐 抓取网页",
        "category": "网络增强",
        "description": "抓取指定 URL 的网页正文内容，用于读取文章、文档、新闻详情等",
        "parameters": {
            "url":      {"type": "string",  "description": "要抓取的网页 URL"},
            "max_len":  {"type": "integer", "description": "返回最大字符数，默认3000", "default": 3000},
        },
        "required": ["url"],
    },
    {
        "name": "weather",
        "display_name": "🌤️ 实时天气",
        "category": "网络增强",
        "description": "查询指定城市的实时天气：温度、体感温度、天气状况、湿度、风速等（无需 API Key）",
        "parameters": {
            "city":   {"type": "string", "description": "城市名称，支持中文或英文，如 '北京' 或 'Beijing'"},
            "days":   {"type": "integer", "description": "预报天数，1=今天，3=三天预报，默认1", "default": 1},
        },
        "required": ["city"],
    },
]


def _parse_bing_html(html: str, max_results: int) -> List[Dict[str, str]]:
    """从 Bing HTML 解析搜索结果"""
    results = []
    # 匹配 Bing 搜索结果条目：<li class="b_algo">
    blocks = re.findall(r'<li class="b_algo">(.*?)</li>', html, re.DOTALL)
    for block in blocks[:max_results]:
        title_m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        desc_m  = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        if title_m:
            url   = title_m.group(1)
            title = re.sub(r'<[^>]+>', '', title_m.group(2)).strip()
            desc  = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
            if url.startswith("http"):
                results.append({"title": title, "url": url, "desc": desc[:200]})
    return results


def _parse_baidu_html(html: str, max_results: int) -> List[Dict[str, str]]:
    """从百度 HTML 解析搜索结果"""
    results = []
    blocks = re.findall(r'<div[^>]+class="[^"]*result[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
    for block in blocks[:max_results * 2]:
        title_m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        desc_m  = re.search(r'class="[^"]*content[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
        if title_m:
            url   = title_m.group(1)
            title = re.sub(r'<[^>]+>', '', title_m.group(2)).strip()
            desc  = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
            if url.startswith("http") and title:
                results.append({"title": title, "url": url, "desc": desc[:200]})
                if len(results) >= max_results:
                    break
    return results


def _format_search_results(query: str, results: List[Dict], engine: str) -> str:
    if not results:
        return f"搜索 '{query}' 未找到结果"
    lines = [f"搜索结果（{query}，来源: {engine}）:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**")
        if r.get("desc"):
            lines.append(f"   {r['desc']}")
        lines.append(f"   🔗 [{r['url']}]({r['url']})\n")
    return "\n".join(lines)


async def execute_web_search(params: dict) -> str:
    query       = params.get("query", "")
    max_results = min(int(params.get("max_results", 5)), 10)
    engine      = params.get("engine", "auto").lower()

    async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
        # 尝试 Bing
        if engine in ("auto", "bing"):
            try:
                resp = await client.get(
                    "https://www.bing.com/search",
                    params={"q": query, "setlang": "zh-CN", "cc": "CN"},
                    headers=_HEADERS,
                )
                results = _parse_bing_html(resp.text, max_results)
                if results:
                    return _format_search_results(query, results, "Bing")
            except Exception:
                pass

        # 备用：百度
        if engine in ("auto", "baidu"):
            try:
                resp = await client.get(
                    "https://www.baidu.com/s",
                    params={"wd": query, "rn": str(max_results)},
                    headers={**_HEADERS, "Referer": "https://www.baidu.com/"},
                )
                results = _parse_baidu_html(resp.text, max_results)
                if results:
                    return _format_search_results(query, results, "百度")
            except Exception:
                pass

    return f"搜索 '{query}' 失败：网络不可达，请检查后端服务器的网络连接"


async def execute_fetch_webpage(params: dict) -> str:
    url     = params.get("url", "")
    max_len = int(params.get("max_len", 3000))
    if not url.startswith("http"):
        return "错误：URL 必须以 http:// 或 https:// 开头"
    try:
        async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
            resp = await client.get(url, headers=_HEADERS)
        html = resp.text
        # 去除 script/style/head
        html = re.sub(r'<(script|style|head)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        # 提取正文文本
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()
        if not text:
            return "未能提取到网页内容"
        suffix = f"\n\n...（内容已截断，原文共 {len(text)} 字符）" if len(text) > max_len else ""
        return f"网页内容（{url}）:\n\n{text[:max_len]}{suffix}"
    except Exception as e:
        return f"抓取网页失败: {e}"


async def execute_weather(params: dict) -> str:
    city = params.get("city", "")
    days = int(params.get("days", 1))
    if not city:
        return "错误：请提供城市名称"
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(
                f"https://wttr.in/{city}",
                params={"format": "j1", "lang": "zh"},
                headers={"User-Agent": "curl/7.68.0"},
            )
        data = resp.json()
        current = data["current_condition"][0]
        location_info = data.get("nearest_area", [{}])[0]
        area_name = location_info.get("areaName", [{}])[0].get("value", city)
        country   = location_info.get("country", [{}])[0].get("value", "")

        temp_c    = current["temp_C"]
        feels_c   = current["FeelsLikeC"]
        humidity  = current["humidity"]
        wind_kmph = current["windspeedKmph"]
        desc      = current.get("lang_zh", [{}])[0].get("value") or current["weatherDesc"][0]["value"]
        visibility = current.get("visibility", "N/A")

        lines = [
            f"📍 {area_name}, {country}",
            f"🌡️ 温度: {temp_c}°C（体感 {feels_c}°C）",
            f"☁️ 天气: {desc}",
            f"💧 湿度: {humidity}%",
            f"💨 风速: {wind_kmph} km/h",
            f"👁️ 能见度: {visibility} km",
        ]

        if days > 1:
            lines.append("\n📅 未来天气预报:")
            for day in data.get("weather", [])[:days]:
                date_str = day["date"]
                max_t    = day["maxtempC"]
                min_t    = day["mintempC"]
                day_desc = day.get("hourly", [{}])[4].get("lang_zh", [{}])[0].get("value") \
                           or day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
                lines.append(f"  {date_str}: {min_t}~{max_t}°C  {day_desc}")

        return "\n".join(lines)
    except Exception as e:
        return f"天气查询失败: {e}（城市名称请尝试英文，如 Beijing）"


HANDLERS = {
    "web_search":    execute_web_search,
    "fetch_webpage": execute_fetch_webpage,
    "weather":       execute_weather,
}

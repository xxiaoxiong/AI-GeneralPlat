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


async def _fetch_page_text(client: httpx.AsyncClient, url: str, max_len: int = 1500) -> str:
    """抓取单个网页的正文文本（用于搜索结果内容增强）"""
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=10)
        html = resp.text
        html = re.sub(r'<(script|style|head|nav|footer|header)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()
        if len(text) < 50:
            return ""
        return text[:max_len]
    except Exception:
        return ""


def _format_search_results(query: str, results: List[Dict], engine: str) -> str:
    if not results:
        return f"搜索 '{query}' 未找到结果"
    lines = [f"搜索结果（{query}，来源: {engine}）:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**")
        if r.get("desc"):
            lines.append(f"   {r['desc']}")
        if r.get("page_content"):
            lines.append(f"   📄 页面摘要: {r['page_content']}")
        lines.append(f"   🔗 [{r['url']}]({r['url']})\n")
    return "\n".join(lines)


def _parse_duckduckgo_html(html: str, max_results: int) -> List[Dict[str, str]]:
    """从 DuckDuckGo HTML Lite 解析搜索结果"""
    results = []

    # DDG HTML Lite 使用 class="result__a" 作为结果链接
    # 模式 1：提取 result 块
    blocks = re.findall(
        r'<div[^>]+class="[^"]*links_main[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL
    )
    if not blocks:
        # 模式 2：匹配 result 级别的 div
        blocks = re.findall(
            r'<div[^>]+class="[^"]*result\b[^"]*"[^>]*>(.*?)</div>\s*(?=<div|$)',
            html, re.DOTALL
        )

    for block in blocks[:max_results]:
        # 提取链接和标题
        link_m = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link_m:
            link_m = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link_m:
            link_m = re.search(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)

        if link_m:
            url = link_m.group(1)
            title = re.sub(r'<[^>]+>', '', link_m.group(2)).strip()
            # 提取描述
            desc = ""
            desc_m = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</[a-z]', block, re.DOTALL)
            if not desc_m:
                desc_m = re.search(r'class="[^"]*snippet[^"]*"[^>]*>(.*?)</[a-z]', block, re.DOTALL)
            if desc_m:
                desc = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip()
            if url.startswith("http") and title and "duckduckgo" not in url.lower():
                results.append({"title": title, "url": url, "desc": desc[:200]})

    # 如果块解析失败，尝试全局提取
    if not results:
        all_links = re.findall(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL
        )
        if not all_links:
            all_links = re.findall(
                r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>',
                html, re.DOTALL
            )
        for url, title_html in all_links[:max_results]:
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            if url.startswith("http") and title:
                results.append({"title": title, "url": url, "desc": ""})

    return results


# 搜索结果自动拓展：拨取前 N 条结果的页面内容
_AUTO_FETCH_TOP_N = 2
_AUTO_FETCH_MAX_CHARS = 1200


async def _enhance_results_with_content(
    client: httpx.AsyncClient, results: List[Dict], top_n: int = _AUTO_FETCH_TOP_N
) -> List[Dict]:
    """自动拓展搜索结果：并发拨取前 top_n 条结果的页面正文，加入到 page_content 字段"""
    import asyncio
    targets = [r for r in results[:top_n] if r.get("url", "").startswith("http")]
    if not targets:
        return results

    tasks = [_fetch_page_text(client, r["url"], _AUTO_FETCH_MAX_CHARS) for r in targets]
    contents = await asyncio.gather(*tasks, return_exceptions=True)

    for r, content in zip(targets, contents):
        if isinstance(content, str) and content:
            r["page_content"] = content
    return results


async def execute_web_search(params: dict) -> str:
    from app.core.config import settings

    query       = params.get("query", "")
    max_results = min(int(params.get("max_results", 5)), 10)
    engine      = params.get("engine", "auto").lower()

    errors = []  # 记录每个引擎的失败原因
    timeout = settings.SEARCH_TIMEOUT or 25
    found_results = None
    found_engine = ""

    async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True) as client:
        # 引擎 0：SearXNG（内网搜索引擎，优先级最高）
        searxng_url = settings.SEARXNG_URL
        if not found_results and searxng_url and engine in ("auto", "searxng"):
            try:
                resp = await client.get(
                    f"{searxng_url.rstrip('/')}/search",
                    params={"q": query, "format": "json", "language": "zh-CN"},
                    headers=_HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = []
                    for item in (data.get("results") or [])[:max_results]:
                        results.append({
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "desc": (item.get("content") or "")[:200],
                        })
                    if results:
                        found_results = results
                        found_engine = "SearXNG"
                    else:
                        errors.append("SearXNG: 返回 0 条结果")
                else:
                    errors.append(f"SearXNG: HTTP {resp.status_code}")
            except httpx.ConnectError:
                errors.append(f"SearXNG: 连接失败（{searxng_url}）")
            except httpx.TimeoutException:
                errors.append("SearXNG: 请求超时")
            except Exception as e:
                errors.append(f"SearXNG: {type(e).__name__}: {e}")

        # 引擎 1：DuckDuckGo HTML Lite（POST 方式，最低反爬虫）
        if not found_results and engine in ("auto", "duckduckgo"):
            try:
                resp = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query},
                    headers={
                        **_HEADERS,
                        "Referer": "https://duckduckgo.com/",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                if resp.status_code == 200:
                    results = _parse_duckduckgo_html(resp.text, max_results)
                    if results:
                        found_results = results
                        found_engine = "DuckDuckGo"
                    else:
                        errors.append(f"DuckDuckGo: 解析到 0 条结果（HTML长度={len(resp.text)}）")
                else:
                    errors.append(f"DuckDuckGo: HTTP {resp.status_code}")
            except httpx.ConnectError:
                errors.append("DuckDuckGo: 连接失败（网络不可达）")
            except httpx.TimeoutException:
                errors.append("DuckDuckGo: 请求超时")
            except Exception as e:
                errors.append(f"DuckDuckGo: {type(e).__name__}: {e}")

        # 引擎 2：Bing
        if not found_results and engine in ("auto", "bing"):
            try:
                resp = await client.get(
                    "https://www.bing.com/search",
                    params={"q": query, "setlang": "zh-CN", "cc": "CN"},
                    headers=_HEADERS,
                )
                results = _parse_bing_html(resp.text, max_results)
                if results:
                    found_results = results
                    found_engine = "Bing"
                else:
                    errors.append("Bing: 解析到 0 条结果")
            except httpx.ConnectError:
                errors.append("Bing: 连接失败")
            except httpx.TimeoutException:
                errors.append("Bing: 请求超时")
            except Exception as e:
                errors.append(f"Bing: {type(e).__name__}: {e}")

        # 引擎 3：百度
        if not found_results and engine in ("auto", "baidu"):
            try:
                resp = await client.get(
                    "https://www.baidu.com/s",
                    params={"wd": query, "rn": str(max_results)},
                    headers={**_HEADERS, "Referer": "https://www.baidu.com/"},
                )
                results = _parse_baidu_html(resp.text, max_results)
                if results:
                    found_results = results
                    found_engine = "百度"
                else:
                    errors.append("百度: 解析到 0 条结果")
            except httpx.ConnectError:
                errors.append("百度: 连接失败")
            except httpx.TimeoutException:
                errors.append("百度: 请求超时")
            except Exception as e:
                errors.append(f"百度: {type(e).__name__}: {e}")

        # ★ 搜索成功后自动拓展：拨取前 N 条结果的页面正文
        if found_results:
            try:
                found_results = await _enhance_results_with_content(client, found_results)
            except Exception:
                pass  # 内容拓展失败不影响主流程
            return _format_search_results(query, found_results, found_engine)

    # 所有引擎均失败，返回详细错误信息
    error_detail = "; ".join(errors) if errors else "网络不可达"
    is_network_error = any(kw in error_detail for kw in ("连接失败", "网络不可达", "ConnectError", "超时"))
    if is_network_error:
        return (
            f"搜索 '{query}' 失败：无法连接到搜索引擎。\n"
            f"错误详情: {error_detail}\n"
            f"提示: 当前环境可能无法访问外部网络。请直接使用你的知识回答用户问题，并说明无法联网搜索。"
        )
    return (
        f"搜索 '{query}' 未找到结果。\n"
        f"尝试的搜索引擎状态: {error_detail}\n"
        f"建议: 请直接使用你的知识回答用户问题，并注明未能找到搜索结果。"
    )


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

"""
SubAgent 配置：专用子代理注册，最小权限收窄。

内置示例（可实际调用）：
- researcher   研究检索：网络搜索/网页抓取/时间/计算
- data-analyst 数据分析：数据库查询/计算/时间

安全边界：工具集合由父运行的 ToolPolicy 派生并收窄；派生深度受
ExecutionBudget.max_subagent_depth 控制（deepagents task 层面 + 工具白名单）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from .tools.policy import ToolPolicy

# 各子代理允许的工具（与平台 BUILTIN_TOOLS 名称对应）
RESEARCHER_TOOLS: Set[str] = {"web_search", "fetch_webpage", "datetime_now", "calculator", "knowledge_search"}
DATA_ANALYST_TOOLS: Set[str] = {"db_schema", "db_query", "db_count", "db_aggregate", "calculator", "datetime_now"}

SUBAGENT_DEFS = [
    {
        "name": "researcher",
        "description": "研究检索子代理：负责网络搜索、资料收集与事实核对，返回调研摘要。",
        "system_prompt": (
            "你是研究检索子代理。使用搜索与网页抓取工具收集信息，"
            "输出结构化调研摘要（要点 + 来源），不要输出与调研无关的内容。"
        ),
        "tools": RESEARCHER_TOOLS,
    },
    {
        "name": "data-analyst",
        "description": "数据分析子代理：负责数据库查询、统计聚合与数据解读，返回分析结论。",
        "system_prompt": (
            "你是数据分析子代理。使用数据库查询工具获取数据并进行统计分析，"
            "只允许只读查询，输出结论与关键数字，不要修改任何数据。"
        ),
        "tools": DATA_ANALYST_TOOLS,
    },
]


def build_subagents(policy: ToolPolicy) -> List[Dict[str, Any]]:
    """按当前运行策略生成 deepagents subagents 配置（工具收窄到交集）。"""
    out = []
    for d in SUBAGENT_DEFS:
        allowed = sorted(d["tools"] & policy.allowed_tools)
        if not allowed:
            continue
        out.append({
            "name": d["name"],
            "description": d["description"],
            "system_prompt": d["system_prompt"],
            "tools": allowed,
        })
    return out

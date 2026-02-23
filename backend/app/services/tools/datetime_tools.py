"""📅 时间日期工具：datetime_now（获取实时时间，LLM 无法自行获取）"""
from datetime import datetime
from typing import Dict, Any, List


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "datetime_now",
        "display_name": "📅 当前时间",
        "category": "时间日期",
        "description": "获取当前日期、时间、星期、时区等实时信息",
        "parameters": {
            "format": {"type": "string", "description": "输出格式: full/date/time/timestamp，默认full", "default": "full"},
        },
        "required": [],
    },
]

WEEK = ["一", "二", "三", "四", "五", "六", "日"]


def execute_datetime_now(params: dict) -> str:
    now = datetime.now()
    fmt = (params or {}).get("format", "full")
    week = WEEK[now.weekday()]
    if fmt == "date":
        return f"{now.strftime('%Y-%m-%d')} 星期{week}"
    if fmt == "time":
        return now.strftime('%H:%M:%S')
    if fmt == "timestamp":
        return str(int(now.timestamp()))
    return (
        f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M:%S')}\n"
        f"星期{week}\n"
        f"Unix时间戳：{int(now.timestamp())}\n"
        f"ISO格式：{now.isoformat()}"
    )


HANDLERS = {
    "datetime_now":  execute_datetime_now,
    "datetime_tool": execute_datetime_now,
}

"""🗄️ 数据库工具：db_query, db_count, db_aggregate"""
import re
from typing import Dict, Any, List

from sqlalchemy import text


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "db_query",
        "display_name": "🗄️ 数据库查询",
        "category": "数据库",
        "description": "执行 SELECT SQL 查询，从数据库获取数据，最多返回50行",
        "parameters": {
            "sql":   {"type": "string",  "description": "SELECT SQL 语句，支持 WHERE/JOIN/GROUP BY 等"},
            "limit": {"type": "integer", "description": "最大返回行数，默认20", "default": 20},
        },
        "required": ["sql"],
    },
    {
        "name": "db_count",
        "display_name": "🗄️ 数据库计数",
        "category": "数据库",
        "description": "统计数据库表中满足条件的记录数量",
        "parameters": {
            "table":     {"type": "string", "description": "表名"},
            "condition": {"type": "string", "description": "WHERE 条件，如 'status=1 AND created_at > \"2024-01-01\"'", "default": "1=1"},
        },
        "required": ["table"],
    },
    {
        "name": "db_aggregate",
        "display_name": "🗄️ 数据库聚合",
        "category": "数据库",
        "description": "对数据库表执行聚合统计：SUM/AVG/MAX/MIN/COUNT，支持分组",
        "parameters": {
            "table":     {"type": "string", "description": "表名"},
            "agg_func":  {"type": "string", "description": "聚合函数: SUM/AVG/MAX/MIN/COUNT"},
            "column":    {"type": "string", "description": "聚合列名，COUNT时可用*"},
            "group_by":  {"type": "string", "description": "分组列名（可选）", "default": ""},
            "condition": {"type": "string", "description": "WHERE 条件（可选）", "default": "1=1"},
        },
        "required": ["table", "agg_func", "column"],
    },
]

_VALID_TABLE = re.compile(r"^[a-zA-Z_]\w*$")
_ALLOWED_AGG = {"SUM", "AVG", "MAX", "MIN", "COUNT"}


async def execute_db_query(db, params: dict) -> str:
    sql = params.get("sql", "").strip()
    limit = min(int(params.get("limit", 20)), 50)
    if not re.match(r"^\s*select\b", sql, re.IGNORECASE):
        return "错误：只允许 SELECT 查询"
    try:
        result = await db.execute(text(sql))
        rows = result.fetchmany(limit)
        cols = list(result.keys())
        if not rows:
            return "查询结果为空（0行）"
        lines = [" | ".join(str(c) for c in cols), "-" * 60]
        lines += [" | ".join(str(v) for v in row) for row in rows]
        lines.append(f"\n共返回 {len(rows)} 行")
        return "\n".join(lines)
    except Exception as e:
        return f"SQL执行错误: {e}"


async def execute_db_count(db, params: dict) -> str:
    table = params.get("table", "")
    condition = params.get("condition", "1=1") or "1=1"
    if not _VALID_TABLE.match(table):
        return "错误：表名不合法（只允许字母、数字、下划线）"
    try:
        result = await db.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE {condition}"))
        count = result.scalar()
        return f"表 `{table}` 满足条件 [{condition}] 的记录数：{count}"
    except Exception as e:
        return f"计数查询错误: {e}"


async def execute_db_aggregate(db, params: dict) -> str:
    table    = params.get("table", "")
    agg_func = params.get("agg_func", "COUNT").upper()
    column   = params.get("column", "*")
    group_by = params.get("group_by", "") or ""
    condition = params.get("condition", "1=1") or "1=1"

    if agg_func not in _ALLOWED_AGG:
        return f"错误：不支持的聚合函数 '{agg_func}'，请用 SUM/AVG/MAX/MIN/COUNT"
    if not _VALID_TABLE.match(table):
        return "错误：表名不合法"

    try:
        sel_prefix = f"{group_by}, " if group_by else ""
        grp_clause = f"GROUP BY {group_by}" if group_by else ""
        sql = (
            f"SELECT {sel_prefix}{agg_func}({column}) AS result "
            f"FROM `{table}` WHERE {condition} {grp_clause} LIMIT 50"
        )
        result = await db.execute(text(sql))
        rows = result.fetchall()
        cols = list(result.keys())
        if not rows:
            return "聚合结果为空"
        lines = [" | ".join(cols), "-" * 40]
        lines += [" | ".join(str(v) for v in row) for row in rows]
        return "\n".join(lines)
    except Exception as e:
        return f"聚合查询错误: {e}"


HANDLERS = {
    "db_query":     execute_db_query,
    "db_count":     execute_db_count,
    "db_aggregate": execute_db_aggregate,
}

"""🗄️ 数据库工具：db_schema, db_query, db_count, db_aggregate

增强功能：
- db_schema: 表结构发现，让模型知道有哪些表和列
- 所有查询结果以 Markdown 表格格式返回，方便前端直接渲染
- SQL 注入防护增强
"""
import re
from typing import Dict, Any, List

from sqlalchemy import text


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "db_schema",
        "display_name": "🗄️ 数据库表结构",
        "category": "数据库",
        "description": "查看数据库中有哪些表，以及指定表的列名和类型。在执行查询前应先了解表结构。",
        "parameters": {
            "table": {"type": "string", "description": "表名（可选）。留空返回所有表列表，填写则返回该表的列信息", "default": ""},
        },
        "required": [],
    },
    {
        "name": "db_query",
        "display_name": "🗄️ 数据库查询",
        "category": "数据库",
        "description": "执行 SELECT SQL 查询，从数据库获取数据，最多返回50行。结果以 Markdown 表格格式返回。",
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
        "description": "对数据库表执行聚合统计：SUM/AVG/MAX/MIN/COUNT，支持分组。结果以 Markdown 表格返回。",
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
# SQL 注入危险关键词
_DANGEROUS_KEYWORDS = re.compile(
    r'\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|EXEC|EXECUTE)\b',
    re.IGNORECASE,
)


def _rows_to_markdown(cols: list, rows: list) -> str:
    """将查询结果转换为 Markdown 表格格式"""
    if not cols or not rows:
        return "查询结果为空（0行）"

    col_names = [str(c) for c in cols]
    header = "| " + " | ".join(col_names) + " |"
    separator = "| " + " | ".join("---" for _ in col_names) + " |"
    data_lines = []
    for row in rows:
        cells = [str(v) if v is not None else "" for v in row]
        data_lines.append("| " + " | ".join(cells) + " |")

    result = "\n".join([header, separator] + data_lines)
    result += f"\n\n共返回 {len(rows)} 行"
    return result


async def execute_db_schema(db, params: dict) -> str:
    """查看数据库表结构：列出所有表或指定表的列信息"""
    table = params.get("table", "").strip()

    try:
        if not table:
            # 列出所有表
            try:
                # MySQL
                result = await db.execute(text("SHOW TABLES"))
                tables = [row[0] for row in result.fetchall()]
            except Exception:
                try:
                    # SQLite
                    result = await db.execute(text(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ))
                    tables = [row[0] for row in result.fetchall()]
                except Exception:
                    # PostgreSQL
                    result = await db.execute(text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' ORDER BY table_name"
                    ))
                    tables = [row[0] for row in result.fetchall()]

            if not tables:
                return "数据库中没有找到任何表"

            lines = ["数据库中共有以下表：\n"]
            for i, t in enumerate(tables, 1):
                lines.append(f"{i}. `{t}`")
            lines.append(f"\n共 {len(tables)} 个表。使用 db_schema 并指定 table 参数可查看表的列信息。")
            return "\n".join(lines)

        else:
            # 查看指定表的列信息
            if not _VALID_TABLE.match(table):
                return "错误：表名不合法（只允许字母、数字、下划线）"

            try:
                # MySQL
                result = await db.execute(text(f"DESCRIBE `{table}`"))
                rows = result.fetchall()
                cols = list(result.keys())
                return f"表 `{table}` 的结构：\n\n" + _rows_to_markdown(cols, rows)
            except Exception:
                try:
                    # SQLite
                    result = await db.execute(text(f"PRAGMA table_info(`{table}`)"))
                    rows = result.fetchall()
                    if not rows:
                        return f"表 `{table}` 不存在或没有列"
                    cols = ["序号", "列名", "类型", "非空", "默认值", "主键"]
                    return f"表 `{table}` 的结构：\n\n" + _rows_to_markdown(cols, rows)
                except Exception:
                    # PostgreSQL
                    result = await db.execute(text(
                        f"SELECT column_name, data_type, is_nullable, column_default "
                        f"FROM information_schema.columns "
                        f"WHERE table_name = '{table}' ORDER BY ordinal_position"
                    ))
                    rows = result.fetchall()
                    cols = ["列名", "类型", "可空", "默认值"]
                    return f"表 `{table}` 的结构：\n\n" + _rows_to_markdown(cols, rows)

    except Exception as e:
        return f"查询表结构错误: {e}"


async def execute_db_query(db, params: dict) -> str:
    """执行 SELECT 查询，返回 Markdown 表格"""
    sql = params.get("sql", "").strip()
    limit = min(int(params.get("limit", 20)), 50)

    if not re.match(r"^\s*select\b", sql, re.IGNORECASE):
        return "错误：只允许 SELECT 查询"

    # SQL 注入防护
    if _DANGEROUS_KEYWORDS.search(sql):
        return "错误：SQL 中包含不允许的关键词（只允许 SELECT 查询）"

    try:
        result = await db.execute(text(sql))
        rows = result.fetchmany(limit)
        cols = list(result.keys())
        if not rows:
            return "查询结果为空（0行）"
        return _rows_to_markdown(cols, rows)
    except Exception as e:
        return f"SQL执行错误: {e}"


async def execute_db_count(db, params: dict) -> str:
    """统计记录数量"""
    table = params.get("table", "")
    condition = params.get("condition", "1=1") or "1=1"
    if not _VALID_TABLE.match(table):
        return "错误：表名不合法（只允许字母、数字、下划线）"
    if _DANGEROUS_KEYWORDS.search(condition):
        return "错误：条件中包含不允许的关键词"
    try:
        result = await db.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE {condition}"))
        count = result.scalar()
        return f"表 `{table}` 满足条件 [{condition}] 的记录数：**{count}**"
    except Exception as e:
        return f"计数查询错误: {e}"


async def execute_db_aggregate(db, params: dict) -> str:
    """执行聚合统计，返回 Markdown 表格"""
    table    = params.get("table", "")
    agg_func = params.get("agg_func", "COUNT").upper()
    column   = params.get("column", "*")
    group_by = params.get("group_by", "") or ""
    condition = params.get("condition", "1=1") or "1=1"

    if agg_func not in _ALLOWED_AGG:
        return f"错误：不支持的聚合函数 '{agg_func}'，请用 SUM/AVG/MAX/MIN/COUNT"
    if not _VALID_TABLE.match(table):
        return "错误：表名不合法"
    if _DANGEROUS_KEYWORDS.search(condition):
        return "错误：条件中包含不允许的关键词"

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
        return _rows_to_markdown(cols, rows)
    except Exception as e:
        return f"聚合查询错误: {e}"


HANDLERS = {
    "db_schema":    execute_db_schema,
    "db_query":     execute_db_query,
    "db_count":     execute_db_count,
    "db_aggregate": execute_db_aggregate,
}

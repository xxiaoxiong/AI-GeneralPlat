"""工具副作用等级与重放决策（FR-05 外部副作用治理）。

任务书治理规则：
- read_only：无副作用，通常允许安全重试；
- external_idempotent：支持下游 Idempotency-Key 的 HTTP POST，重试时复用同一幂等键；
- non_idempotent：不支持幂等的下单/扣款/发通知等，不确定时禁止自动重放。

重放决策 ``replay_action`` 是恢复层（service._recovery_check）与
工具层（tools/adapter）的唯一共同判定来源。
"""
from __future__ import annotations

from ..repositories.tool_execution_repository import (
    TOOL_EXEC_FAILED,
    TOOL_EXEC_NEEDS_RECONCILIATION,
    TOOL_EXEC_PREPARED,
    TOOL_EXEC_SUCCEEDED,
)

CLASS_READ_ONLY = "read_only"
CLASS_EXTERNAL_IDEMPOTENT = "external_idempotent"
CLASS_NON_IDEMPOTENT = "non_idempotent"

READ_ONLY_TOOLS = frozenset({
    "calculator",
    "datetime_now",
    "db_aggregate",
    "db_count",
    "db_query",
    "db_schema",
    "fetch_webpage",
    "http_get",
    "knowledge_search",
    "weather",
    "web_search",
})

# 支持下游 Idempotency-Key Header 去重的副作用工具
EXTERNAL_IDEMPOTENT_TOOLS = frozenset({"http_post"})

# 显式登记的不可幂等副作用工具（未知工具名默认按 non_idempotent 处理）
NON_IDEMPOTENT_TOOLS = frozenset({"python_exec", "send_email_tool", "webhook_notify"})

REPLAY_CACHED = "cached"    # 已成功：返回持久化结果，不重复执行
REPLAY_RETRY = "retry"      # 允许重试（确定失败 / read_only / 幂等键下游）
REPLAY_BLOCK = "block"      # 结果未知：禁止自动重放，进入核对


def tool_outcome_class(name: str) -> str:
    if name in READ_ONLY_TOOLS:
        return CLASS_READ_ONLY
    if name in EXTERNAL_IDEMPOTENT_TOOLS:
        return CLASS_EXTERNAL_IDEMPOTENT
    return CLASS_NON_IDEMPOTENT


def replay_action(status: str, tool_name: str) -> str:
    """已有账本记录时的重放决策。

    - succeeded：返回缓存结果；
    - prepared / failed：失败语义明确（未产生或确认未产生副作用），允许重试；
    - executing / unknown：结果不确定 —— read_only 无副作用可重试，
      external_idempotent 复用同一幂等键重试（下游去重），其余禁止重放；
    - needs_reconciliation：人工核对后才允许继续。
    """
    if status == TOOL_EXEC_SUCCEEDED:
        return REPLAY_CACHED
    if status in (TOOL_EXEC_PREPARED, TOOL_EXEC_FAILED):
        return REPLAY_RETRY
    if status == TOOL_EXEC_NEEDS_RECONCILIATION:
        return REPLAY_BLOCK
    cls = tool_outcome_class(tool_name)
    if cls == CLASS_READ_ONLY:
        return REPLAY_RETRY
    if cls == CLASS_EXTERNAL_IDEMPOTENT:
        return REPLAY_RETRY
    return REPLAY_BLOCK

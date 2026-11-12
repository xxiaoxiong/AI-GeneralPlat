"""持久化层异常约定。"""
from langgraph.errors import GraphBubbleUp


class PersistenceError(RuntimeError):
    """状态/账本持久化失败：调用方必须中止推进，不允许继续产生副作用。"""


class IdempotencyConflict(Exception):
    """幂等键已用于不同请求内容（request_hash 不一致）。"""


class RunLeaseLost(GraphBubbleUp):
    """执行权丢失（租约易主 / 租约过期）：调用方必须立即停止推进（P0-02）。

    收到该异常时不得继续 Session 写回、不得发布成功/失败终态事件、
    不得产生新的工具副作用；数据库中当前有效 Worker 的状态原样保留。

    继承 GraphBubbleUp：从工具协程抛出时穿透 LangGraph ToolNode 的通用异常
    吞噬，不得被降级为普通工具错误消息（任务书 §5.2 D）。
    """


class IllegalTransition(GraphBubbleUp):
    """非法状态迁移（终态覆盖 / expected_status 不匹配 / 状态机拒绝）（P0-02）。

    收到该异常时不得写库覆盖既有状态（尤其是终态），调用方必须显式处理。
    继承 GraphBubbleUp 以穿透 ToolNode 吞噬（Tool Ledger 状态冲突必须被
    Adapter 感知并中止 Graph，不得静默）。
    """


class ToolCallIdentityConflict(PersistenceError, GraphBubbleUp):
    """调用身份冲突：同 (run_id, tool_call_id) 已有记录但 tool_name /
    args_hash / idempotency_key 不一致（任务书 §5.2 B）。

    任何不一致都不得返回旧缓存结果、不得继续执行；必须显式冲突中止。
    """

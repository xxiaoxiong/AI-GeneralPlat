"""持久化层异常约定。"""


class PersistenceError(RuntimeError):
    """状态/账本持久化失败：调用方必须中止推进，不允许继续产生副作用。"""


class IdempotencyConflict(Exception):
    """幂等键已用于不同请求内容（request_hash 不一致）。"""


class RunLeaseLost(Exception):
    """执行权丢失（租约易主 / 租约过期）：调用方必须立即停止推进（P0-02）。

    收到该异常时不得继续 Session 写回、不得发布成功/失败终态事件、
    不得产生新的工具副作用；数据库中当前有效 Worker 的状态原样保留。
    """


class IllegalTransition(Exception):
    """非法状态迁移（终态覆盖 / expected_status 不匹配 / 状态机拒绝）（P0-02）。

    收到该异常时不得写库覆盖既有状态（尤其是终态），调用方必须显式处理。
    """

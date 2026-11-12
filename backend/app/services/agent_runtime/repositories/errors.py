"""持久化异常：关键状态写入失败必须向上抛出，禁止只记日志后继续。"""
from __future__ import annotations


class PersistenceError(Exception):
    """关键状态持久化失败（数据库不可用/写入失败）。"""


class IdempotencyConflict(Exception):
    """同一幂等键、不同请求摘要：不得静默复用已有 Run。"""

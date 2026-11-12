"""工具副作用等级、幂等契约声明与恢复决策表（FR-05 / P0-04 §6）。

任务书治理规则：
- read_only：无副作用，通常允许安全重试；
- external_idempotent：仅限**工具契约明确声明**下游支持 Idempotency-Key 去重
  的目标（§6.2 B）——未声明的 HTTP POST 一律按 non_idempotent 处理；
- non_idempotent：不支持幂等的下单/扣款/发通知等，不确定时禁止自动重放。

两个决策函数是恢复层（service._recovery_check）与工具层（tools/adapter）的
唯一共同判定来源：

- ``replay_action``：已有账本记录时的重放决策；
- ``recovery_decision``：恢复决策表（§6.2 A）——Checkpoint 存在/缺失/损坏
  与账本未决记录的组合策略，禁止在 Checkpoint 缺失时重新推理生成新调用 ID。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..lifecycle import (
    RECOVERY_CONTINUE,
    RECOVERY_FRESH,
    RECOVERY_NEEDS_RECONCILIATION,
)
from ..repositories.tool_execution_repository import (
    TOOL_EXEC_EXECUTING,
    TOOL_EXEC_FAILED,
    TOOL_EXEC_NEEDS_RECONCILIATION,
    TOOL_EXEC_PREPARED,
    TOOL_EXEC_SUCCEEDED,
    TOOL_EXEC_UNKNOWN,
)

CLASS_READ_ONLY = "read_only"
CLASS_EXTERNAL_IDEMPOTENT = "external_idempotent"
CLASS_NON_IDEMPOTENT = "non_idempotent"

RETRY_SAFE = "safe_retry"                  # 无副作用：可安全重试
RETRY_IDEMPOTENT_KEY = "idempotent_key"    # 复用同一幂等键重试（下游去重）
RETRY_NONE = "none"                        # 结果未知时禁止自动重试


# 契约声明（§6.2 B）：副作用等级与下游幂等能力必须由工具配置显式声明，
# 未声明的工具一律按 non_idempotent 处理（默认拒绝自动重放）。
@dataclass(frozen=True)
class SideEffectContract:
    """工具副作用契约（§6.2 B，由工具配置/部署显式声明）。

    - ``side_effect_class``：read_only / external_idempotent / non_idempotent；
    - ``downstream_idempotency_supported``：下游是否真实执行幂等键去重；
    - ``idempotency_key_header``：下游幂等键 Header 名（未声明则不注入）；
    - ``retry_policy``：结果未知时的重试策略；
    - ``external_operation_lookup``：外部操作结果核对方式（人工核对线索）。
    """

    tool_name: str
    side_effect_class: str = CLASS_NON_IDEMPOTENT
    downstream_idempotency_supported: bool = False
    idempotency_key_header: Optional[str] = None
    retry_policy: str = RETRY_NONE
    external_operation_lookup: Optional[str] = None


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

# 显式登记的不可幂等副作用工具（其余未声明工具按默认 non_idempotent 处理）
NON_IDEMPOTENT_TOOLS = frozenset({
    "python_exec",
    "send_email_tool",
    "webhook_notify",
    "write_file",
    "edit_file",
    "delete_file",
})

REPLAY_CACHED = "cached"    # 已成功：返回持久化结果，不重复执行
REPLAY_RETRY = "retry"      # 允许重试（确定失败 / read_only / 幂等键下游）
REPLAY_BLOCK = "block"      # 结果未知：禁止自动重放，进入核对

# 副作用可能已发生但结果未确认的状态（"未决副作用"，恢复决策表语义）。
# prepared 只是执行前登记、尚未产生副作用；failed/succeeded 已确认终结。
_UNCONFIRMED_SIDE_EFFECT = (
    TOOL_EXEC_EXECUTING,
    TOOL_EXEC_UNKNOWN,
    TOOL_EXEC_NEEDS_RECONCILIATION,
)


# ── 契约注册表（默认拒绝：未声明 = non_idempotent）────────────────────────

_CONTRACTS: Dict[str, SideEffectContract] = {}


def register_side_effect_contract(contract: SideEffectContract) -> None:
    """登记工具副作用契约（由工具配置 / 部署在启动时声明）。

    只有明确声明 ``downstream_idempotency_supported=True`` 且给出
    ``idempotency_key_header`` 的工具才允许标记为 external_idempotent ——
    否则抛 ValueError，防止把任意 HTTP POST 误判为下游幂等。
    """
    if contract.side_effect_class == CLASS_EXTERNAL_IDEMPOTENT and not (
        contract.downstream_idempotency_supported and contract.idempotency_key_header
    ):
        raise ValueError(
            f"工具 {contract.tool_name} 声明 external_idempotent 必须同时声明 "
            "downstream_idempotency_supported=True 与 idempotency_key_header"
        )
    if contract.side_effect_class == CLASS_READ_ONLY and contract.tool_name not in READ_ONLY_TOOLS:
        # read_only 声明需要与已知只读工具一致（防止副作用工具被误声明为只读）
        raise ValueError(
            f"工具 {contract.tool_name} 不在 READ_ONLY_TOOLS 中，不得声明 read_only"
        )
    _CONTRACTS[contract.tool_name] = contract


def unregister_side_effect_contract(tool_name: str) -> None:
    """移除契约声明（恢复默认 non_idempotent；测试/配置重载用）。"""
    _CONTRACTS.pop(tool_name, None)


def get_side_effect_contract(tool_name: str) -> SideEffectContract:
    """查询工具副作用契约；未声明时默认 non_idempotent（禁止自动重放）。

    已知只读工具默认按 read_only 契约（safe_retry）；其余未声明工具
    （含 HTTP POST）一律 non_idempotent（§6.2 B）。
    """
    contract = _CONTRACTS.get(tool_name)
    if contract is not None:
        return contract
    if tool_name in READ_ONLY_TOOLS:
        return SideEffectContract(
            tool_name=tool_name,
            side_effect_class=CLASS_READ_ONLY,
            retry_policy=RETRY_SAFE,
        )
    return SideEffectContract(tool_name=tool_name)  # 默认 non_idempotent


def idempotency_key_header(tool_name: str) -> Optional[str]:
    """下游幂等键 Header 名；未声明下游幂等能力的工具返回 None（不注入）。"""
    contract = get_side_effect_contract(tool_name)
    if contract.downstream_idempotency_supported and contract.idempotency_key_header:
        return contract.idempotency_key_header
    return None


def tool_outcome_class(name: str) -> str:
    return get_side_effect_contract(name).side_effect_class


def replay_action(status: str, tool_name: str) -> str:
    """已有账本记录时的重放决策。

    - succeeded：返回缓存结果；
    - prepared / failed：失败语义明确（未产生或确认未产生副作用），允许重试；
    - executing / unknown：结果不确定 —— read_only 无副作用可重试，
      **契约声明**下游幂等的工具复用同一幂等键重试（下游去重），其余禁止重放；
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


# ── 恢复决策表（P0-04 §6.2 A）────────────────────────────────────────────

CHECKPOINT_OK = "ok"                  # 存在且可读
CHECKPOINT_MISSING = "missing"        # 不存在
CHECKPOINT_UNREADABLE = "unreadable"  # 读取异常 / 已损坏，执行进度无法确认


@dataclass(frozen=True)
class RecoveryDecision:
    """恢复决策结果（写入日志与 agent_runs.recovery_metadata）。"""

    mode: str
    reason: str
    checkpoint_state: str
    pending: List[Dict[str, Any]] = field(default_factory=list)


def _pending_summary(entries: Sequence[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "tool_call_id": getattr(e, "tool_call_id", None),
            "tool_name": e.tool_name,
            "status": e.status,
            "outcome_class": tool_outcome_class(e.tool_name),
        }
        for e in entries
    ]


def recovery_decision(checkpoint_state: str, entries: Sequence[Any]) -> RecoveryDecision:
    """恢复决策表（§6.2 A，RC-01~RC-05）。

    ===============  ==========================  ============================================
    Checkpoint       Ledger 状态                  恢复策略
    ===============  ==========================  ============================================
    存在且可读       全部已确认                   从 Checkpoint 继续
    存在且可读       read_only 未决               允许安全重试（继续）
    存在且可读       幂等契约声明的未决            仅复用原 key 重试（继续；原 tool_call_id
                                                 由 Checkpoint 保持）
    存在且可读       non_idempotent 未决           needs_reconciliation
    不存在           无未决副作用                 按输入快照重新开始（记录 recovery mode）
    不存在           read_only 未决               允许安全重试（重新开始）
    不存在           external_idempotent 未决      needs_reconciliation（禁止重新推理生成
                                                 新调用 ID）
    不存在           non_idempotent 未决           needs_reconciliation
    读取异常/损坏    无法确认恢复安全性            安全阻断 + recovery_error，绝不 fresh
    ===============  ==========================  ============================================

    "未决副作用"指结果未确认（executing / unknown / needs_reconciliation）的
    记录；prepared 仅是执行前登记、尚未产生副作用，不阻断恢复。
    """
    pending = _pending_summary(entries)

    def _block(reason: str) -> RecoveryDecision:
        return RecoveryDecision(RECOVERY_NEEDS_RECONCILIATION, reason, checkpoint_state, pending)

    if checkpoint_state == CHECKPOINT_UNREADABLE:
        return _block(
            "Checkpoint 不可读/已损坏，执行进度无法确认，安全阻断"
            "（禁止 fresh restart、禁止自动重放），需人工核对"
        )

    unconfirmed = [e for e in entries if e.status in _UNCONFIRMED_SIDE_EFFECT]

    if checkpoint_state == CHECKPOINT_MISSING:
        if not unconfirmed:
            return RecoveryDecision(
                RECOVERY_FRESH,
                "无 Checkpoint 且无结果未知的副作用，按输入快照重新开始（recovery mode=fresh）",
                checkpoint_state,
                pending,
            )
        risky = [
            e for e in unconfirmed
            if e.status == TOOL_EXEC_NEEDS_RECONCILIATION
            or tool_outcome_class(e.tool_name) != CLASS_READ_ONLY
        ]
        if risky:
            return _block(
                "无 Checkpoint 且存在结果未知的副作用（external_idempotent/non_idempotent），"
                "无法恢复原 tool_call_id/幂等键，禁止重新推理生成新调用 ID、禁止自动重放，"
                "进入人工核对"
            )
        return RecoveryDecision(
            RECOVERY_FRESH,
            "无 Checkpoint，结果未知的记录均为 read_only（无副作用），允许安全重试"
            "（recovery mode=fresh）",
            checkpoint_state,
            pending,
        )

    # CHECKPOINT_OK
    if any(replay_action(e.status, e.tool_name) == REPLAY_BLOCK for e in entries):
        return _block("存在结果未知的工具副作用（执行账本未决），禁止自动重放，需人工对账")
    return RecoveryDecision(
        RECOVERY_CONTINUE,
        "Checkpoint 可读，从断点继续（重放复用原 tool_call_id 与幂等键）",
        checkpoint_state,
        pending,
    )

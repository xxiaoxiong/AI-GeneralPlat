"""统一工具执行结果（任务书 P0-05 / §7.2 A）。

所有工具 Handler 与 ToolExecutor 的统一返回契约：

- ``succeeded``：远端/本地已确认执行成功；
- ``failed``：确认失败（含参数校验失败、只读工具失败、远端业务错误码）；
- ``unknown``：**无法确认外部操作是否已生效**（超时、连接中断等）——
  不是普通失败，禁止无脑重试，须经副作用等级判定/人工核对。

注意：普通返回字符串视为 succeeded（业务错误必须显式返回
``ToolResult.failed(...)``，禁止把错误伪装成成功字符串，§7.1）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"


@dataclass
class ToolResult:
    """统一工具执行结果（§7.2 A）。"""

    status: str
    output: Any = None
    error: Optional[str] = None
    retryable: bool = False
    outcome_known: bool = True
    external_operation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def succeeded(
        cls,
        output: Any = None,
        *,
        external_operation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ToolResult":
        return cls(
            status=STATUS_SUCCEEDED,
            output=output,
            outcome_known=True,
            external_operation_id=external_operation_id,
            metadata=metadata or {},
        )

    @classmethod
    def failed(
        cls,
        error: Optional[str] = None,
        *,
        retryable: bool = False,
        output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ToolResult":
        """确认失败：确认未产生副作用或结果不生效（§7.2 B）。"""
        return cls(
            status=STATUS_FAILED,
            error=error,
            output=output,
            retryable=retryable,
            outcome_known=True,
            metadata=metadata or {},
        )

    @classmethod
    def unknown(
        cls,
        error: Optional[str] = None,
        *,
        output: Any = None,
        external_operation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ToolResult":
        """结果未知：无法确认外部操作是否已生效（§7.2 B）。"""
        return cls(
            status=STATUS_UNKNOWN,
            error=error,
            output=output,
            outcome_known=False,
            external_operation_id=external_operation_id,
            metadata=metadata or {},
        )

    @property
    def text(self) -> str:
        """给模型/调用方的文本渲染。"""
        if self.status == STATUS_SUCCEEDED:
            if self.output is None:
                return ""
            if isinstance(self.output, str):
                return self.output
            return json.dumps(self.output, ensure_ascii=False, default=str)
        return str(self.error or self.output or self.status)


def normalize(ret: Any) -> ToolResult:
    """边界归一化：任意 handler 返回值 -> ToolResult。

    - ToolResult 原样返回；
    - 普通字符串/其他值视为 succeeded（业务错误必须显式 ToolResult.failed）；
    - None 视为无输出成功。
    """
    if isinstance(ret, ToolResult):
        return ret
    if ret is None:
        return ToolResult.succeeded("")
    return ToolResult.succeeded(ret)

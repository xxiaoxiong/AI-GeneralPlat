"""
基础可观测：运行事件 / trace 导出钩子（任务书 P0-H）。

平台侧只定义最小记录模型与导出接口，不绑定具体观测后端：
- 默认 LoggingExporter 把记录打进 `agent_runtime.telemetry` 日志；
- Langfuse / OpenTelemetry 为预留接入点：实现 ``TelemetryExporter.export``
  并通过 ``telemetry.add_exporter(...)`` 注册即可，例如：
  - Langfuse：在 export 内用其 SDK 创建 trace/span（fields 里有 trace_id/run_id）；
  - OTel：把 record 映射为 span event / span（kind 区分 run/tool）。

记录字段（对齐平台日志规范）：
  request_id / trace_id / run_id / thread_id / agent_id /
  tenant_id / user_id / session_id / model_id / 耗时 / 工具状态
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger("agent_runtime.telemetry")

KIND_RUN = "run"
KIND_TOOL = "tool"


@dataclass
class TelemetryRecord:
    """一条可观测记录（run 级或 tool 级）。"""

    kind: str                       # run / tool
    name: str                       # run_id 或工具名
    status: str                     # running / completed / failed / cancelled / limit_exceeded / rejected ...
    fields: Dict[str, Any] = field(default_factory=dict)
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
            **self.fields,
            **({"attributes": self.attributes} if self.attributes else {}),
        }


@runtime_checkable
class TelemetryExporter(Protocol):
    """导出器接口：Langfuse / OTel 等后端接入点。"""

    def export(self, record: TelemetryRecord) -> None: ...


class LoggingExporter:
    """默认导出器：结构化日志（便于 grep / 采集到日志平台）。"""

    def export(self, record: TelemetryRecord) -> None:
        logger.info("telemetry %s", record.to_dict())


class Telemetry:
    """运行/工具记录汇聚点（进程内单例 `telemetry`）。"""

    def __init__(self, exporters: Optional[List[TelemetryExporter]] = None) -> None:
        self._exporters: List[TelemetryExporter] = list(exporters or [LoggingExporter()])

    def add_exporter(self, exporter: TelemetryExporter) -> None:
        self._exporters.append(exporter)

    def clear_exporters(self) -> None:
        self._exporters.clear()

    # ── 记录入口 ──────────────────────────────────────────────────────────

    def record_run(
        self,
        fields: Dict[str, Any],
        *,
        status: str,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._emit(TelemetryRecord(
            kind=KIND_RUN,
            name=str(fields.get("run_id") or ""),
            status=status,
            fields=dict(fields),
            duration_ms=duration_ms,
            error=error,
            attributes=attributes or {},
        ))

    def record_tool(
        self,
        fields: Dict[str, Any],
        *,
        tool: str,
        status: str,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._emit(TelemetryRecord(
            kind=KIND_TOOL,
            name=tool,
            status=status,
            fields=dict(fields),
            duration_ms=duration_ms,
            error=error,
            attributes=attributes or {},
        ))

    def _emit(self, record: TelemetryRecord) -> None:
        for exporter in self._exporters:
            try:
                exporter.export(record)
            except Exception:  # 观测失败不得影响执行
                logger.exception("telemetry exporter 导出失败")


telemetry = Telemetry()

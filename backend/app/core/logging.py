"""
结构化日志系统：支持 Agent 执行轨迹、工具调用、性能指标的结构化记录
"""
import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional
from contextvars import ContextVar

from app.core.config import settings

# ── 上下文变量：追踪请求级别信息 ──────────────────────────────────────────────
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")
agent_id_var: ContextVar[str] = ContextVar("agent_id", default="")


class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器，输出 JSON 格式日志"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 注入上下文变量
        trace_id = trace_id_var.get("")
        if trace_id:
            log_entry["trace_id"] = trace_id
        session_id = session_id_var.get("")
        if session_id:
            log_entry["session_id"] = session_id
        agent_id = agent_id_var.get("")
        if agent_id:
            log_entry["agent_id"] = agent_id

        # 注入附加字段
        extra_data = getattr(record, "extra_data", None)
        if extra_data is not None:
            log_entry["data"] = extra_data

        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class AgentLogger:
    """Agent 专用日志器，提供结构化日志方法"""

    def __init__(self, name: str = "agent"):
        self._logger = logging.getLogger(f"ai_plat.{name}")

    def _log(self, level: int, msg: str, extra_data: Optional[Dict] = None, **kwargs):
        record = self._logger.makeRecord(
            self._logger.name, level, "(agent)", 0, msg, (), None
        )
        if extra_data:
            setattr(record, "extra_data", extra_data)
        self._logger.handle(record)

    def info(self, msg: str, **data):
        self._log(logging.INFO, msg, extra_data=data if data else None)

    def warning(self, msg: str, **data):
        self._log(logging.WARNING, msg, extra_data=data if data else None)

    def error(self, msg: str, **data):
        self._log(logging.ERROR, msg, extra_data=data if data else None)

    def debug(self, msg: str, **data):
        self._log(logging.DEBUG, msg, extra_data=data if data else None)

    # ── Agent 专用日志方法 ────────────────────────────────────────────────────

    def trace_step(self, iteration: int, step_type: str, content: str, **extra):
        """记录 Agent 执行步骤"""
        if not settings.LOG_AGENT_TRACE:
            return
        self._log(logging.INFO, f"[Step {iteration}] {step_type}", extra_data={
            "iteration": iteration,
            "step_type": step_type,
            "content": content[:500],
            **extra,
        })

    def tool_call(self, tool_name: str, tool_input: Dict, duration_ms: float = 0,
                  success: bool = True, result_preview: str = ""):
        """记录工具调用"""
        if not settings.LOG_TOOL_CALLS:
            return
        self._log(logging.INFO, f"Tool: {tool_name}", extra_data={
            "tool_name": tool_name,
            "tool_input": tool_input,
            "duration_ms": round(duration_ms, 2),
            "success": success,
            "result_preview": result_preview[:200],
        })

    def performance(self, operation: str, duration_ms: float, **metrics):
        """记录性能指标"""
        if not settings.LOG_PERFORMANCE:
            return
        level = logging.WARNING if duration_ms > settings.MONITOR_SLOW_THRESHOLD_MS else logging.INFO
        self._log(level, f"Perf: {operation} ({duration_ms:.1f}ms)", extra_data={
            "operation": operation,
            "duration_ms": round(duration_ms, 2),
            **metrics,
        })

    def hallucination_check(self, check_type: str, passed: bool, details: str = ""):
        """记录幻觉检查结果"""
        level = logging.WARNING if not passed else logging.DEBUG
        self._log(level, f"Hallucination check: {check_type}", extra_data={
            "check_type": check_type,
            "passed": passed,
            "details": details[:300],
        })

    def memory_event(self, event_type: str, **data):
        """记录记忆系统事件"""
        self._log(logging.INFO, f"Memory: {event_type}", extra_data={
            "event_type": event_type,
            **data,
        })

    def context_event(self, event_type: str, **data):
        """记录上下文管理事件"""
        self._log(logging.DEBUG, f"Context: {event_type}", extra_data={
            "event_type": event_type,
            **data,
        })


class PerformanceTimer:
    """性能计时器上下文管理器"""

    def __init__(self, operation: str, logger: Optional[AgentLogger] = None):
        self.operation = operation
        self.logger = logger or get_logger("perf")
        self.start_time = 0.0
        self.duration_ms = 0.0

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.duration_ms = (time.perf_counter() - self.start_time) * 1000
        self.logger.performance(self.operation, self.duration_ms)


def setup_logging():
    """初始化日志系统"""
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger("ai_plat")
    root_logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

    # 控制台 handler（简洁格式）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(console_handler)

    # 文件 handler（JSON 结构化）
    file_handler = logging.FileHandler(
        str(log_dir / "agent.log"), encoding="utf-8"
    )
    file_handler.setFormatter(StructuredFormatter())
    root_logger.addHandler(file_handler)

    # Agent 轨迹专用文件
    if settings.LOG_AGENT_TRACE:
        trace_handler = logging.FileHandler(
            str(log_dir / "agent_trace.log"), encoding="utf-8"
        )
        trace_handler.setFormatter(StructuredFormatter())
        trace_logger = logging.getLogger("ai_plat.agent")
        trace_logger.addHandler(trace_handler)


def get_logger(name: str = "agent") -> AgentLogger:
    """获取 Agent 日志器"""
    return AgentLogger(name)

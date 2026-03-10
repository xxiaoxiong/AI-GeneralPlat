"""
执行轨迹追踪与性能监控

核心功能：
1. Agent 执行轨迹回溯 → 能复盘每一步
2. 性能监控 → 速度、失败率、调用次数
3. 确定性执行支持 → 同样输入 → 同样输出
4. 意图漂移检测 → 追踪任务是否偏离
"""
import time
import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("trace")


@dataclass
class TraceStep:
    """单个执行步骤"""
    step_id: int
    step_type: str          # thought | action | observation | final | error | verify
    content: str
    timestamp: float = 0
    duration_ms: float = 0
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "content": self.content[:500],
            "duration_ms": round(self.duration_ms, 2),
            "metadata": self.metadata,
        }


@dataclass
class ExecutionTrace:
    """完整执行轨迹"""
    trace_id: str
    agent_id: Optional[int] = None
    session_id: Optional[int] = None
    user_id: Optional[int] = None
    original_task: str = ""
    steps: List[TraceStep] = field(default_factory=list)
    start_time: float = 0
    end_time: float = 0
    total_iterations: int = 0
    total_tool_calls: int = 0
    total_tokens_used: int = 0
    final_answer: str = ""
    success: bool = True
    error: str = ""

    def add_step(self, step_type: str, content: str, **metadata) -> TraceStep:
        step = TraceStep(
            step_id=len(self.steps) + 1,
            step_type=step_type,
            content=content,
            timestamp=time.time(),
            metadata=metadata,
        )
        self.steps.append(step)
        return step

    @property
    def duration_ms(self) -> float:
        if self.end_time and self.start_time:
            return (self.end_time - self.start_time) * 1000
        return 0

    def to_dict(self) -> Dict:
        return {
            "trace_id": self.trace_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "original_task": self.original_task[:200],
            "total_iterations": self.total_iterations,
            "total_tool_calls": self.total_tool_calls,
            "total_tokens_used": self.total_tokens_used,
            "duration_ms": round(self.duration_ms, 2),
            "success": self.success,
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
            "final_answer_preview": self.final_answer[:200],
        }

    def to_summary(self) -> str:
        """生成人类可读的执行摘要"""
        lines = [
            f"执行轨迹 [{self.trace_id}]",
            f"任务: {self.original_task[:100]}",
            f"状态: {'✅ 成功' if self.success else '❌ 失败'}",
            f"耗时: {self.duration_ms:.0f}ms",
            f"迭代: {self.total_iterations} 次",
            f"工具调用: {self.total_tool_calls} 次",
            f"Token: ~{self.total_tokens_used}",
            "",
            "步骤:",
        ]
        for step in self.steps:
            icon = {
                "thought": "💭",
                "action": "🔧",
                "observation": "👁",
                "final": "✅",
                "error": "❌",
                "verify": "🔍",
            }.get(step.step_type, "📝")
            lines.append(f"  {icon} [{step.step_id}] {step.step_type}: {step.content[:80]}")

        return "\n".join(lines)


class PerformanceMonitor:
    """性能监控器"""

    def __init__(self):
        self.metrics: Dict[str, List[float]] = {
            "inference_latency_ms": [],
            "tool_latency_ms": [],
            "tokens_per_second": [],
            "total_latency_ms": [],
        }
        self._counters: Dict[str, Any] = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "tool_calls": 0,
            "tool_failures": 0,
            "hallucination_detections": 0,
            "retry_count": 0,
        }

    def record_inference(self, latency_ms: float, tokens: int = 0):
        """记录推理性能"""
        self.metrics["inference_latency_ms"].append(latency_ms)
        if tokens > 0 and latency_ms > 0:
            tps = tokens / (latency_ms / 1000)
            self.metrics["tokens_per_second"].append(tps)

    def record_tool_call(self, latency_ms: float, success: bool):
        """记录工具调用性能"""
        self.metrics["tool_latency_ms"].append(latency_ms)
        self._counters["tool_calls"] += 1
        if not success:
            self._counters["tool_failures"] += 1

    def record_request(self, latency_ms: float, success: bool):
        """记录请求性能"""
        self.metrics["total_latency_ms"].append(latency_ms)
        self._counters["total_requests"] += 1
        if success:
            self._counters["successful_requests"] += 1
        else:
            self._counters["failed_requests"] += 1

    def increment(self, counter_name: str, value: int = 1):
        """增加计数器"""
        self._counters[counter_name] = self._counters.get(counter_name, 0) + value

    def get_summary(self) -> Dict[str, Any]:
        """获取性能摘要"""
        summary = dict(self._counters)

        for metric_name, values in self.metrics.items():
            if values:
                summary[f"{metric_name}_avg"] = round(sum(values) / len(values), 2)
                summary[f"{metric_name}_p95"] = round(
                    sorted(values)[int(len(values) * 0.95)] if len(values) >= 20 else max(values), 2
                )
                summary[f"{metric_name}_max"] = round(max(values), 2)

        # 计算成功率
        total = self._counters["total_requests"]
        if total > 0:
            summary["success_rate"] = round(
                self._counters["successful_requests"] / total * 100, 1
            )

        # 工具成功率
        tool_total = self._counters["tool_calls"]
        if tool_total > 0:
            summary["tool_success_rate"] = round(
                (tool_total - self._counters["tool_failures"]) / tool_total * 100, 1
            )

        # 检查是否达到性能目标（≥25 token/s）
        tps_values = self.metrics.get("tokens_per_second", [])
        if tps_values:
            avg_tps = sum(tps_values) / len(tps_values)
            summary["avg_tokens_per_second"] = round(avg_tps, 1)
            summary["meets_speed_target"] = avg_tps >= 25

        return summary


class IntentDriftDetector:
    """意图漂移检测器"""

    def __init__(self, original_task: str):
        self.original_task = original_task
        self.original_keywords = self._extract_keywords(original_task)

    def check_drift(self, current_thought: str) -> bool:
        """
        检测当前思考是否偏离原始任务

        返回 True 表示检测到漂移
        """
        if not self.original_keywords:
            return False

        current_keywords = self._extract_keywords(current_thought)
        if not current_keywords:
            return False

        # 计算关键词重叠率
        overlap = len(self.original_keywords & current_keywords)
        overlap_ratio = overlap / max(len(self.original_keywords), 1)

        # 重叠率低于 10% 时认为可能漂移
        if overlap_ratio < 0.1 and len(current_thought) > 50:
            logger.warning(
                "检测到意图漂移",
                original_task=self.original_task[:100],
                current_thought=current_thought[:100],
                overlap_ratio=overlap_ratio,
            )
            return True

        return False

    @staticmethod
    def _extract_keywords(text: str) -> set:
        """提取关键词"""
        import re
        # 中文关键词
        chinese = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
        # 英文关键词
        english = set(w.lower() for w in re.findall(r'[a-zA-Z]{3,}', text))
        # 过滤停用词
        stopwords = {"the", "and", "for", "that", "this", "with", "from", "are", "was",
                     "是", "的", "了", "在", "有", "和", "不", "这", "那", "我", "你",
                     "他", "们", "要", "会", "可以", "需要", "使用", "进行", "一个"}
        return (chinese | english) - stopwords


# ── 全局性能监控实例 ─────────────────────────────────────────────────────────

_global_monitor = PerformanceMonitor()


def get_global_monitor() -> PerformanceMonitor:
    return _global_monitor

"""
断点续跑与错误恢复

核心功能：
1. 断点保存 → 每个 ReAct 步骤后保存状态
2. 断点恢复 → 崩溃后能从最近断点恢复
3. 错误恢复 → 模型输出乱了要能救回来
"""
import json
import time
import os
from typing import Dict, Any, Optional, List
from pathlib import Path
from datetime import datetime

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("checkpoint")


class CheckpointManager:
    """
    断点续跑管理器

    每个 ReAct 迭代步骤结束后保存检查点，包含：
    - 当前迭代次数
    - 工作内存
    - 工具调用历史
    - 已收集的 Observation
    - 当前状态（thinking/acting/observing/done）
    """

    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.checkpoint_dir = Path(settings.CHECKPOINT_DIR)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_path = self.checkpoint_dir / f"{trace_id}.json"

    def save(
        self,
        iteration: int,
        working_memory: List[Dict],
        tool_history: List[Dict],
        observations: List[Dict],
        state: str = "running",
        extra: Optional[Dict] = None,
    ):
        """保存检查点"""
        if not settings.CHECKPOINT_ENABLED:
            return

        checkpoint = {
            "trace_id": self.trace_id,
            "iteration": iteration,
            "state": state,
            "working_memory": working_memory,
            "tool_history": tool_history,
            "observations": observations,
            "extra": extra or {},
            "saved_at": datetime.utcnow().isoformat(),
        }

        try:
            with open(self._checkpoint_path, "w", encoding="utf-8") as f:
                json.dump(checkpoint, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"保存检查点失败: {e}")

    def load(self) -> Optional[Dict]:
        """加载检查点"""
        if not settings.CHECKPOINT_ENABLED:
            return None

        if not self._checkpoint_path.exists():
            return None

        try:
            with open(self._checkpoint_path, "r", encoding="utf-8") as f:
                checkpoint = json.load(f)
            logger.info(f"加载检查点: iteration={checkpoint.get('iteration')}")
            return checkpoint
        except Exception as e:
            logger.error(f"加载检查点失败: {e}")
            return None

    def clear(self):
        """清除检查点"""
        try:
            if self._checkpoint_path.exists():
                self._checkpoint_path.unlink()
        except Exception:
            pass

    @staticmethod
    def cleanup_old_checkpoints(max_age_hours: int = 24):
        """清理过期检查点"""
        checkpoint_dir = Path(settings.CHECKPOINT_DIR)
        if not checkpoint_dir.exists():
            return

        now = time.time()
        max_age_seconds = max_age_hours * 3600

        for f in checkpoint_dir.glob("*.json"):
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink()
            except Exception:
                pass


class ErrorRecovery:
    """
    错误恢复策略

    处理场景：
    1. 模型输出格式错误 → 重新提示格式要求
    2. 工具执行失败 → 重试或跳过
    3. 上下文溢出 → 压缩历史
    4. 死循环检测 → 强制终止
    """

    def __init__(self):
        self.error_count = 0
        self.max_consecutive_errors = 3
        self.recent_outputs: List[str] = []  # 用于检测死循环

    def handle_parse_error(self, raw_output: str, iteration: int) -> Dict[str, Any]:
        """处理解析错误"""
        self.error_count += 1

        if self.error_count >= self.max_consecutive_errors:
            return {
                "action": "force_answer",
                "message": "连续多次输出格式错误，请直接给出最终回答",
                "raw_fallback": raw_output,
            }

        return {
            "action": "retry_with_format_hint",
            "message": (
                "你的输出格式不正确。请严格按以下格式输出：\n\n"
                "如果需要工具：\n"
                "Thought: [分析]\n"
                "Action: [工具名]\n"
                'Action Input: {"参数名": "参数值"}\n\n'
                "如果直接回答：\n"
                "Thought: [总结]\n"
                "Final Answer: [回答内容]"
            ),
        }

    def handle_tool_error(self, tool_name: str, error: str, retry_count: int) -> Dict[str, Any]:
        """处理工具执行错误"""
        if retry_count >= settings.AGENT_MAX_TOOL_RETRIES:
            return {
                "action": "skip_tool",
                "message": f"工具 {tool_name} 多次执行失败，请用已有信息回答或换一个工具。\n错误: {error}",
            }

        return {
            "action": "retry",
            "message": f"工具 {tool_name} 执行失败: {error}\n请检查参数后重试。",
        }

    def detect_loop(self, current_output: str) -> bool:
        """
        检测死循环：如果最近 3 次输出高度相似，判定为死循环
        """
        self.recent_outputs.append(current_output[:200])
        if len(self.recent_outputs) > 5:
            self.recent_outputs = self.recent_outputs[-5:]

        if len(self.recent_outputs) >= 3:
            last3 = self.recent_outputs[-3:]
            # 计算两两相似度
            similarities = []
            for i in range(len(last3)):
                for j in range(i + 1, len(last3)):
                    sim = _text_similarity(last3[i], last3[j])
                    similarities.append(sim)
            avg_sim = sum(similarities) / len(similarities) if similarities else 0
            if avg_sim > 0.85:
                logger.warning("检测到死循环", similarity=avg_sim)
                return True

        return False

    def reset_error_count(self):
        """重置错误计数（当成功执行一步时调用）"""
        self.error_count = 0


def _text_similarity(a: str, b: str) -> float:
    """简单文本相似度（基于字符重叠）"""
    if not a or not b:
        return 0.0
    set_a = set(a)
    set_b = set(b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0

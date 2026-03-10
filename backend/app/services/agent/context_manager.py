"""
上下文管理器：决定 Agent 能否 "不掉线"

核心功能：
1. 滑动窗口策略 → 保留最近 N 轮完整对话
2. 摘要压缩策略 → 旧历史变 1~2 句话
3. 关键信息抽取 → 只保留用户要求、约束、结果
4. 动态上下文长度 → 任务简单就短，复杂就长
5. 上下文填充顺序 → 最新内容放最靠近模型输入处
6. 原始任务永远置顶 → 防止意图漂移
7. 上下文噪声过滤 → 旧内容不干扰新决策
"""
import re
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("context")


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数（中文约 1.5 token/字，英文约 1.3 token/词）
    比调用 tokenizer 快，适合实时决策
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    ascii_words = len(re.findall(r'[a-zA-Z0-9]+', text))
    other = len(text) - chinese_chars - sum(len(w) for w in re.findall(r'[a-zA-Z0-9]+', text))
    return int(chinese_chars * 1.5 + ascii_words * 1.3 + other * 0.5)


@dataclass
class ContextSlot:
    """上下文槽位，用于管理不同优先级的上下文内容"""
    name: str
    content: str
    priority: int          # 优先级（越高越重要，越不会被裁剪）
    pinned: bool = False   # 是否置顶（永不裁剪）
    tokens: int = 0        # 预估 token 数

    def __post_init__(self):
        if self.tokens == 0:
            self.tokens = estimate_tokens(self.content)


@dataclass
class ManagedContext:
    """管理后的上下文结果"""
    messages: List[Dict[str, str]]     # 最终发送给模型的消息列表
    total_tokens: int                   # 预估总 token 数
    original_task: str                  # 原始任务（始终保留）
    summary: str = ""                   # 历史摘要
    dropped_turns: int = 0             # 被裁剪的对话轮数
    context_strategy: str = ""         # 使用的策略


class ContextManager:
    """
    上下文窗口管理器

    上下文填充顺序（从模型输入最远到最近）：
    1. [PIN] 系统提示词
    2. [PIN] 原始任务描述
    3. 历史摘要（压缩后的旧对话）
    4. 记忆上下文（相关长期记忆）
    5. 最近 N 轮完整对话（滑动窗口）
    6. 当前 ReAct 工作内存（最新的 Thought/Action/Observation）
    """

    def __init__(
        self,
        max_context_tokens: int = 0,
        sliding_window_turns: int = 0,
        summary_threshold: int = 0,
    ):
        self.max_context_tokens = max_context_tokens or settings.INFERENCE_MAX_CONTEXT_TOKENS
        self.sliding_window_turns = sliding_window_turns or settings.CONTEXT_SLIDING_WINDOW_TURNS
        self.summary_threshold = summary_threshold or settings.CONTEXT_SUMMARY_THRESHOLD

    def build_context(
        self,
        system_prompt: str,
        chat_history: List[Dict[str, str]],
        working_memory: List[Dict[str, str]],
        original_task: str = "",
        memory_context: str = "",
        history_summary: str = "",
    ) -> ManagedContext:
        """
        构建受管理的上下文，确保不超过 token 限制

        Args:
            system_prompt: 系统提示词
            chat_history: 完整对话历史 [{"role": "user/assistant", "content": "..."}]
            working_memory: 当前 ReAct 步骤 [{"role": "...", "content": "..."}]
            original_task: 用户的原始任务
            memory_context: 长期记忆上下文
            history_summary: 已有的历史摘要
        """
        # 计算各部分预估 token
        system_tokens = estimate_tokens(system_prompt)
        reserve_for_output = settings.INFERENCE_MAX_OUTPUT_TOKENS
        available_tokens = self.max_context_tokens - system_tokens - reserve_for_output

        if available_tokens <= 0:
            logger.warning("上下文预算不足", system_tokens=system_tokens,
                         max_tokens=self.max_context_tokens)
            available_tokens = 2048  # 最低保障

        # ── 第 1 步：置顶内容（不可裁剪）────────────────────────────────────
        pinned_messages = []
        pinned_tokens = 0

        # 原始任务置顶
        if original_task and settings.CONTEXT_PIN_ORIGINAL_TASK:
            task_msg = f"【原始任务】{original_task}"
            pinned_messages.append({"role": "user", "content": task_msg})
            pinned_tokens += estimate_tokens(task_msg)

        # 记忆上下文
        if memory_context:
            mem_tokens = estimate_tokens(memory_context)
            max_mem_tokens = min(mem_tokens, available_tokens // 4)  # 记忆最多占 1/4
            if mem_tokens > max_mem_tokens:
                memory_context = self._truncate_to_tokens(memory_context, max_mem_tokens)
            pinned_messages.append({"role": "system", "content": memory_context})
            pinned_tokens += estimate_tokens(memory_context)

        remaining_tokens = available_tokens - pinned_tokens

        # ── 第 2 步：历史摘要（如果有）──────────────────────────────────────
        summary_messages = []
        if history_summary:
            summary_tokens = estimate_tokens(history_summary)
            max_summary_tokens = min(summary_tokens, settings.CONTEXT_MAX_HISTORY_TOKENS)
            if summary_tokens > max_summary_tokens:
                history_summary = self._truncate_to_tokens(history_summary, max_summary_tokens)
            summary_msg = f"【历史摘要】{history_summary}"
            summary_messages.append({"role": "system", "content": summary_msg})
            remaining_tokens -= estimate_tokens(summary_msg)

        # ── 第 3 步：工作内存（当前 ReAct 步骤，最高优先级）────────────────
        working_tokens = sum(estimate_tokens(m.get("content", "")) for m in working_memory)

        # 工作内存至少保留 50% 预算
        min_working_budget = remaining_tokens // 2
        history_budget = remaining_tokens - max(working_tokens, min_working_budget)

        # ── 第 4 步：滑动窗口选取历史对话 ──────────────────────────────────
        selected_history, dropped = self._select_history(
            chat_history, history_budget
        )

        # ── 第 5 步：组装最终消息列表 ──────────────────────────────────────
        final_messages = []

        # 置顶内容（原始任务、记忆）
        final_messages.extend(pinned_messages)

        # 历史摘要
        final_messages.extend(summary_messages)

        # 滑动窗口内的历史对话
        final_messages.extend(selected_history)

        # 当前 ReAct 工作内存
        final_messages.extend(working_memory)

        total_tokens = system_tokens + sum(
            estimate_tokens(m.get("content", "")) for m in final_messages
        )

        strategy = self._describe_strategy(
            len(chat_history), len(selected_history), dropped,
            bool(history_summary), total_tokens
        )

        logger.context_event(
            "context_built",
            total_tokens=total_tokens,
            max_tokens=self.max_context_tokens,
            history_turns=len(chat_history),
            selected_turns=len(selected_history),
            dropped_turns=dropped,
            strategy=strategy,
        )

        return ManagedContext(
            messages=final_messages,
            total_tokens=total_tokens,
            original_task=original_task,
            summary=history_summary,
            dropped_turns=dropped,
            context_strategy=strategy,
        )

    def _select_history(
        self,
        chat_history: List[Dict[str, str]],
        token_budget: int,
    ) -> Tuple[List[Dict[str, str]], int]:
        """
        滑动窗口选取历史对话

        策略：
        1. 优先保留最近 N 轮（滑动窗口）
        2. 在预算内尽量多保留
        3. 过滤噪声内容（纯 ReAct 中间步骤等）
        """
        if not chat_history:
            return [], 0

        # 过滤噪声
        filtered = self._filter_noise(chat_history)

        # 按轮次分组（user + assistant = 1 轮）
        turns = self._group_turns(filtered)

        # 从最近的轮次开始选取
        selected_turns = []
        used_tokens = 0
        max_turns = self.sliding_window_turns

        for turn in reversed(turns):
            turn_tokens = sum(estimate_tokens(m.get("content", "")) for m in turn)
            if used_tokens + turn_tokens > token_budget:
                break
            if len(selected_turns) >= max_turns:
                break
            selected_turns.insert(0, turn)
            used_tokens += turn_tokens

        # 展平为消息列表
        selected_messages = [m for turn in selected_turns for m in turn]
        dropped = len(turns) - len(selected_turns)

        return selected_messages, dropped

    def _filter_noise(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """过滤上下文噪声"""
        filtered = []
        for m in messages:
            content = m.get("content", "")
            role = m.get("role", "")

            # 跳过空消息
            if not content.strip():
                continue

            # 过滤 assistant 消息中的 ReAct 中间步骤（只保留最终回答）
            if role == "assistant":
                # 如果包含 Final Answer，只保留 Final Answer 部分
                fa_match = re.search(r"Final\s*Answer\s*[:：]\s*([\s\S]+?)$", content, re.IGNORECASE)
                if fa_match:
                    content = fa_match.group(1).strip()
                elif re.match(r'\s*Thought\s*[:：]', content, re.IGNORECASE):
                    # 纯 ReAct 中间步骤，跳过
                    continue

                # 过滤历史中的图片/视频链接
                content = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'[媒体已省略: \1]', content)

            filtered.append({"role": role, "content": content})

        return filtered

    def _group_turns(self, messages: List[Dict[str, str]]) -> List[List[Dict[str, str]]]:
        """将消息按对话轮次分组"""
        turns = []
        current_turn = []
        for m in messages:
            current_turn.append(m)
            if m.get("role") == "assistant":
                turns.append(current_turn)
                current_turn = []
        if current_turn:
            turns.append(current_turn)
        return turns

    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """截断文本到指定 token 数"""
        if estimate_tokens(text) <= max_tokens:
            return text
        # 粗略按字符比例截断
        ratio = max_tokens / max(estimate_tokens(text), 1)
        target_len = int(len(text) * ratio * 0.9)  # 留 10% 余量
        return text[:target_len] + "...[已截断]"

    def _describe_strategy(
        self, total_turns: int, selected_turns: int,
        dropped: int, has_summary: bool, total_tokens: int
    ) -> str:
        """描述使用的上下文管理策略"""
        parts = []
        if dropped > 0:
            parts.append(f"滑动窗口(保留{selected_turns}轮,裁剪{dropped}轮)")
        if has_summary:
            parts.append("历史摘要压缩")
        parts.append(f"总计~{total_tokens}tokens")
        return " + ".join(parts) if parts else "完整保留"

    def should_summarize(self, chat_history: List[Dict[str, str]]) -> bool:
        """判断是否需要触发历史摘要"""
        total_tokens = sum(estimate_tokens(m.get("content", "")) for m in chat_history)
        return total_tokens > self.summary_threshold

    def extract_key_info(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        从对话历史中抽取关键信息

        抽取内容：
        - 用户核心需求
        - 约束条件
        - 工具调用结果摘要
        - 已确认的结论
        """
        key_info = {
            "user_requirements": [],
            "constraints": [],
            "tool_results": [],
            "conclusions": [],
        }

        for m in messages:
            content = m.get("content", "")
            role = m.get("role", "")

            if role == "user":
                # 提取需求（第一条用户消息通常是核心需求）
                if not key_info["user_requirements"]:
                    key_info["user_requirements"].append(content[:200])

                # 提取约束条件
                constraint_patterns = [
                    r'(?:必须|一定要|不能|不要|不可以|请注意|限制|要求|约束)[^。！？\n]{2,50}',
                    r'(?:不超过|至少|最多|最少|在\d+[^。！？\n]{0,20}之内)',
                ]
                for p in constraint_patterns:
                    matches = re.findall(p, content)
                    key_info["constraints"].extend(matches[:3])

            elif role == "assistant":
                # 提取 Final Answer 作为结论
                fa_match = re.search(r"Final\s*Answer\s*[:：]\s*([\s\S]+?)$", content, re.IGNORECASE)
                if fa_match:
                    conclusion = fa_match.group(1).strip()[:200]
                    key_info["conclusions"].append(conclusion)

        return key_info

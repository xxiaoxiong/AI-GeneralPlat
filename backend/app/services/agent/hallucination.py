"""
幻觉抑制模块：30B 模型最大弱点的防线

核心功能：
1. 事实校验机制 → 模型不能瞎编
2. 来源引用机制 → 回答必须来自工具结果
3. 不确定性表达 → 不懂要承认
4. 禁止编造工具返回 → 30B 最容易犯的错
5. 多步骤验证 → 重要结果必须验证两次
"""
import re
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("hallucination")


@dataclass
class VerificationResult:
    """校验结果"""
    passed: bool
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    confidence: float = 1.0


class HallucinationGuard:
    """
    幻觉防护系统

    检查层级：
    1. 工具结果伪造检测 → 检测 Final Answer 中是否编造了工具未返回的数据
    2. 来源可追溯性 → 关键数据点必须有工具来源
    3. 自洽性检查 → 回答前后是否矛盾
    4. 不确定性标注 → 自动添加置信度标记
    """

    @staticmethod
    def verify_against_observations(
        final_answer: str,
        observations: List[Dict[str, str]],
    ) -> VerificationResult:
        """
        验证 Final Answer 是否与工具观察结果一致

        核心规则：
        - 如果使用了工具，Final Answer 中的关键数据必须可以追溯到 Observation
        - 不允许编造工具未返回的具体数字、日期、URL
        """
        issues = []
        suggestions = []

        if not observations:
            # 没有工具调用，跳过此检查
            return VerificationResult(passed=True, confidence=0.8)

        # 从 observations 中提取所有数据点
        obs_data_points = set()
        for obs in observations:
            content = obs.get("content", "")
            # 提取数字
            obs_data_points.update(re.findall(r'\b\d+\.?\d*\b', content))
            # 提取 URL
            obs_data_points.update(re.findall(r'https?://\S+', content))
            # 提取日期
            obs_data_points.update(re.findall(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', content))

        # 检查 Final Answer 中的具体数据是否可追溯
        answer_urls = re.findall(r'https?://\S+', final_answer)
        for url in answer_urls:
            url_clean = url.rstrip(')')
            found_in_obs = any(url_clean in obs.get("content", "") for obs in observations)
            if not found_in_obs:
                issues.append(f"URL 可能编造: {url_clean[:80]}")
                suggestions.append("该 URL 未出现在工具返回结果中，请确认真实性")

        # 检查具体数字是否可追溯（只检查看起来像精确数据的数字）
        answer_numbers = re.findall(r'\b(\d{4,})\b', final_answer)  # 4位以上的数字
        for num in answer_numbers:
            found_in_obs = any(num in obs.get("content", "") for obs in observations)
            # 也检查在问题上下文中常见的数字（年份等）不误报
            if not found_in_obs and not _is_common_number(num):
                issues.append(f"精确数字可能编造: {num}")

        passed = len(issues) == 0
        confidence = 1.0 - len(issues) * 0.15

        result = VerificationResult(
            passed=passed,
            issues=issues,
            suggestions=suggestions,
            confidence=max(confidence, 0.3),
        )

        logger.hallucination_check(
            "observation_verify",
            passed=passed,
            details="; ".join(issues) if issues else "OK",
        )

        return result

    @staticmethod
    def check_fabricated_tool_results(
        llm_output: str,
        actual_observations: List[str],
    ) -> VerificationResult:
        """
        检测模型是否编造了 Observation

        30B 最常见的幻觉：自己伪造一个 Observation 然后据此回答
        """
        issues = []

        # 检查 LLM 输出中是否自己生成了 Observation
        fake_obs_patterns = [
            r'Observation\s*[:：]\s*\{[\s\S]*?\}',
            r'Observation\s*[:：]\s*[\[{]',
            r'工具返回\s*[:：]',
            r'结果\s*[:：]\s*\{',
        ]

        for pattern in fake_obs_patterns:
            matches = re.findall(pattern, llm_output, re.IGNORECASE)
            for match in matches:
                # 检查这个 Observation 是否真的来自工具
                is_real = any(match.strip() in obs for obs in actual_observations)
                if not is_real:
                    issues.append(f"疑似编造工具结果: {match[:100]}")

        passed = len(issues) == 0
        result = VerificationResult(passed=passed, issues=issues)

        if not passed:
            logger.hallucination_check(
                "fabricated_tool_result",
                passed=False,
                details="; ".join(issues),
            )

        return result

    @staticmethod
    def check_self_consistency(
        thoughts: List[str],
        final_answer: str,
    ) -> VerificationResult:
        """
        自洽性检查：Thought 链和 Final Answer 是否一致

        检测场景：
        - Thought 说"信息不足"但 Final Answer 给了具体数据
        - Thought 说"需要用工具"但直接给了 Final Answer
        """
        issues = []

        if not thoughts:
            return VerificationResult(passed=True)

        last_thought = thoughts[-1].lower() if thoughts else ""

        # 检查：Thought 表示不确定但 Final Answer 却很确定
        uncertainty_markers = ["不确定", "不清楚", "无法确定", "信息不足", "不够", "需要更多"]
        has_uncertainty = any(m in last_thought for m in uncertainty_markers)

        if has_uncertainty:
            # Final Answer 应该也表达不确定性
            answer_lower = final_answer.lower()
            certainty_check = any(m in answer_lower for m in [
                "可能", "大约", "不确定", "建议", "无法确认", "待验证",
                "根据已有信息", "目前无法", "暂时"
            ])
            if not certainty_check and len(final_answer) > 50:
                issues.append("Thought 表示不确定，但 Final Answer 过于确定")

        passed = len(issues) == 0
        return VerificationResult(passed=passed, issues=issues, confidence=0.9 if passed else 0.6)

    @staticmethod
    def add_uncertainty_markers(text: str, confidence: float) -> str:
        """
        根据置信度为文本添加不确定性标记

        当 confidence < 0.7 时，在关键断言前添加限定词
        """
        if not settings.HALLUCINATION_UNCERTAINTY_EXPR:
            return text
        if confidence >= 0.7:
            return text

        # 低置信度时添加前缀提醒
        prefix = ""
        if confidence < 0.4:
            prefix = "⚠️ 以下信息可能不够准确，仅供参考：\n\n"
        elif confidence < 0.7:
            prefix = "ℹ️ 以下信息基于有限的资料，请注意核实：\n\n"

        return prefix + text

    @staticmethod
    def enforce_source_citation(
        final_answer: str,
        observations: List[Dict[str, str]],
    ) -> str:
        """
        强制来源引用：在使用工具数据时添加来源标注

        如果答案中使用了工具返回的数据，但没有说明来源，
        自动追加来源引用部分
        """
        if not settings.HALLUCINATION_FORCE_SOURCE:
            return final_answer
        if not observations:
            return final_answer

        # 检查答案中是否已经有来源引用
        has_citation = any(marker in final_answer for marker in [
            "来源", "根据", "参考", "数据来自", "工具返回", "查询结果",
            "搜索结果", "Source", "Reference"
        ])

        if has_citation:
            return final_answer

        # 构建来源引用
        sources = []
        for i, obs in enumerate(observations):
            tool_name = obs.get("tool_name", f"工具{i + 1}")
            content_preview = obs.get("content", "")[:100]
            sources.append(f"- {tool_name}: {content_preview}")

        if sources:
            citation = "\n\n---\n📌 **信息来源**：\n" + "\n".join(sources)
            return final_answer + citation

        return final_answer


def _is_common_number(num_str: str) -> bool:
    """判断数字是否是常见的非数据性数字（年份、版本号等）"""
    try:
        n = int(num_str)
        # 年份
        if 1900 <= n <= 2030:
            return True
        # 常见端口号
        if n in (80, 443, 8080, 8000, 8001, 3000, 5173, 3306, 6379):
            return True
        # HTTP 状态码
        if 100 <= n <= 599:
            return True
    except ValueError:
        pass
    return False


class SelfVerifier:
    """
    自我校验器：让模型检查自己的输出

    工作流程：
    1. Agent 生成 Final Answer
    2. SelfVerifier 生成校验 prompt
    3. 模型对自己的输出进行二次检查
    4. 如果发现问题，触发修正
    """

    @staticmethod
    def should_verify(
        final_answer: str,
        observations: List[Dict],
        iteration_count: int,
    ) -> bool:
        """
        判断是否需要自我校验

        30B 优化：减少不必要的 verify 调用，因为：
        - verify 本身消耗一次模型调用（慢+耗 token）
        - 30B 模型的 verify 结果经常比原文更差（截断/丢失信息）
        - 只在真正需要时才触发
        """
        if not settings.AGENT_ENABLE_SELF_VERIFY:
            return False

        # 必须有工具观察结果才值得校验
        if not observations:
            return False

        # 只在多步工具调用 + 答案较长时才校验
        if iteration_count >= 4 and len(final_answer) > 500:
            return True

        # 答案中包含可疑的具体数据（非年份的长数字）且工具调用多次
        has_specific_data = bool(re.search(r'\b\d{6,}\b', final_answer))
        if has_specific_data and len(observations) >= 2:
            return True

        return False

    @staticmethod
    def build_verify_prompt(
        original_question: str,
        final_answer: str,
        observations: List[Dict],
    ) -> str:
        """构建自我校验提示词（30B 优化：简洁版）"""
        obs_summary = ""
        if observations:
            obs_parts = []
            for obs in observations[:3]:  # 最多3条观察，节省 token
                tool = obs.get("tool_name", "")
                content = obs.get("content", "")[:200]
                obs_parts.append(f"[{tool}] {content}")
            obs_summary = "\n".join(obs_parts)

        return (
            f"检查回答准确性：\n"
            f"问题: {original_question[:200]}\n"
            f"回答: {final_answer[:400]}\n"
            f"工具数据:\n{obs_summary}\n\n"
            f"回答正确则输出 VERIFIED\n"
            f"有问题则输出 ISSUE: [问题] 然后输出完整的 Final Answer: [修正后的完整回答]"
        )

    @staticmethod
    def parse_verify_result(verify_output: str) -> Tuple[bool, str]:
        """
        解析校验结果

        30B 优化：增加长度保护，拒绝截断的修正
        """
        if "VERIFIED" in verify_output.upper():
            return True, ""

        # 提取修正后的 Final Answer
        fa_match = re.search(r'Final\s*Answer\s*[:：]\s*([\s\S]+?)$', verify_output, re.IGNORECASE)
        corrected = fa_match.group(1).strip() if fa_match else ""

        # 如果修正文本太短（<50字），可能是截断的，不采用
        if corrected and len(corrected) < 50:
            return False, ""

        # 提取问题描述
        issue_match = re.search(r'ISSUE\s*[:：]\s*(.*?)(?=Final Answer|$)', verify_output, re.DOTALL | re.IGNORECASE)
        issue = issue_match.group(1).strip() if issue_match else ""

        return False, corrected or ""

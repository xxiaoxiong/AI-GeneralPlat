"""Q&A 编排器：在模型调用前进行任务分流、问题重写与执行策略生成。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class QAPlan:
    intent: str
    complexity: str
    rewritten_query: str
    answer_contract: str
    retrieval_k: int
    should_clarify: bool = False
    clarify_question: str = ""
    clarify_suggestions: List[str] | None = None
    intent_confidence: float = 0.5


def _detect_intent(text: str) -> str:
    lower = text.lower()
    if any(k in lower for k in ("sql", "数据库", "表", "db", "字段", "where", "join")):
        return "database"
    if any(k in lower for k in ("总结", "摘要", "归纳", "总结一下", "tl;dr")):
        return "summarization"
    if any(k in lower for k in ("为什么", "原理", "分析", "对比", "优化", "trade-off", "架构")):
        return "analysis"
    if any(k in lower for k in ("写", "生成", "草稿", "文案", "润色")):
        return "generation"
    return "general"


def _intent_confidence(intent: str, text: str) -> float:
    lower = text.lower()
    keywords = {
        "database": ("sql", "数据库", "表", "db", "字段", "where", "join"),
        "summarization": ("总结", "摘要", "归纳", "tl;dr"),
        "analysis": ("为什么", "原理", "分析", "对比", "优化", "trade-off", "架构"),
        "generation": ("写", "生成", "草稿", "文案", "润色"),
    }
    if intent not in keywords:
        return 0.45
    hit = sum(1 for k in keywords[intent] if k in lower)
    if hit >= 2:
        return 0.9
    if hit == 1:
        return 0.72
    return 0.55


def _detect_complexity(text: str) -> str:
    score = 0
    if len(text) > 80:
        score += 1
    if len(re.findall(r"[，。；：,.!?]", text)) >= 3:
        score += 1
    if any(k in text for k in ("并且", "同时", "另外", "步骤", "架构", "逐个", "方面", "收益", "风险")):
        score += 1
    if len(re.findall(r"[0-9一二三四五六七八九十]+个|方面|维度", text)) > 0:
        score += 1
    if score >= 2:
        return "high"
    if score >= 1:
        return "medium"
    return "low"


def _is_followup_message(message: str, history: List[Dict[str, str]]) -> bool:
    """识别短追问，避免在已有上下文下过度追问。"""
    if not history:
        return False
    clean = re.sub(r"\s+", "", message)
    followup_words = ("那", "这个", "这个方案", "继续", "然后", "下一步", "展开", "细说", "具体点", "怎么做")
    return len(clean) <= 14 and any(w in clean for w in followup_words)


def _clarify_suggestions(intent: str) -> List[str]:
    if intent == "database":
        return ["我要查订单表", "按最近7天筛选", "返回字段: id/状态/金额"]
    if intent == "analysis":
        return ["目标是提升效果", "约束是成本不增加", "请输出方案+风险"]
    if intent == "generation":
        return ["受众是技术团队", "语气专业简洁", "长度控制在500字"]
    return ["补充目标", "补充约束", "补充输入范围"]


def _needs_clarification(message: str, intent: str, history: List[Dict[str, str]]) -> tuple[bool, str, List[str]]:
    clean = re.sub(r"\s+", "", message)
    vague_phrases = ("查一下", "看一下", "处理一下", "优化一下", "帮我做", "搞一下")

    # 短追问通常依赖上文，不应机械触发追问
    if _is_followup_message(message, history):
        return False, "", []

    if len(clean) <= 6:
        return True, "为了准确完成任务，请补充你的目标、输入数据范围和期望输出格式。", _clarify_suggestions(intent)

    if any(v in clean for v in vague_phrases):
        if intent == "database" and not any(k in clean for k in ("表", "字段", "条件", "时间", "订单", "用户")):
            return True, "你希望查哪个表/主题？筛选条件是什么？需要哪些字段？", _clarify_suggestions(intent)
        if intent in ("analysis", "general") and len(clean) < 18:
            return True, "请补充：你最关心的目标、约束条件、以及希望我给出的结果形式（方案/步骤/代码）。", _clarify_suggestions(intent)

    return False, "", []


def _rewrite_query(text: str, intent: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if intent == "database":
        return f"数据库任务：{text}。请优先识别表结构、字段约束、过滤条件和时间范围。"
    if intent == "analysis":
        return f"分析任务：{text}。请按问题拆解→证据→结论→可执行建议输出。"
    if intent == "summarization":
        return f"总结任务：{text}。请保留事实，不新增信息，优先保留结论与行动项。"
    if intent == "generation":
        return f"生成任务：{text}。请先确认受众、语气和长度约束。"
    return text


def _answer_contract(intent: str, complexity: str) -> str:
    base = [
        "- 先给结论，再给关键依据。",
        "- 如果存在不确定性，明确标注“待确认”。",
        "- 不要暴露 Thought/Action 等中间推理格式。",
    ]
    if complexity == "high":
        base.append("- 对复杂问题按 1/2/3 分段，最后附“下一步建议”。")
    if intent == "database":
        base.append("- 涉及数据时优先给出 SQL/表字段依据，不得编造记录。")
    if intent == "analysis":
        base.append("- 至少给出 2 个可执行优化项，并说明收益与风险。")
    if intent == "summarization":
        base.append("- 摘要仅保留原文事实与结论，不加入新观点。")
    return "\n".join(base)


def _retrieval_k_by_complexity(complexity: str, intent: str) -> int:
    if intent == "database":
        return 4
    if complexity == "high":
        return 8
    if complexity == "medium":
        return 6
    return 4


def build_qa_plan(message: str, history: List[Dict[str, str]]) -> QAPlan:
    # 融合最近两轮上下文做意图判定，降低单轮抖动
    context_tail = " ".join([m.get("content", "") for m in history[-4:]])
    basis = f"{context_tail}\n{message}".strip()

    intent = _detect_intent(basis)
    confidence = _intent_confidence(intent, basis)
    complexity = _detect_complexity(message)
    should_clarify, clarify_question, clarify_suggestions = _needs_clarification(message, intent, history)

    rewritten_query = _rewrite_query(message, intent)
    answer_contract = _answer_contract(intent, complexity)
    retrieval_k = _retrieval_k_by_complexity(complexity, intent)

    return QAPlan(
        intent=intent,
        complexity=complexity,
        rewritten_query=rewritten_query,
        answer_contract=answer_contract,
        retrieval_k=retrieval_k,
        should_clarify=should_clarify,
        clarify_question=clarify_question,
        clarify_suggestions=clarify_suggestions,
        intent_confidence=confidence,
    )

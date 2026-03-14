import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_build_qa_plan_database_intent():
    from app.services.agent.qa_orchestrator import build_qa_plan

    plan = build_qa_plan("帮我查一下数据库里订单表最近7天数据", [])
    assert plan.intent == "database"
    assert "数据库任务" in plan.rewritten_query
    assert "不得编造记录" in plan.answer_contract
    assert plan.retrieval_k == 4


def test_build_qa_plan_high_complexity_analysis():
    from app.services.agent.qa_orchestrator import build_qa_plan

    msg = "请从架构、检索、记忆、提示词、评估五个方面逐个分析并给出优化建议，同时说明收益和风险。"
    plan = build_qa_plan(msg, [{"role": "user", "content": "我们在做问答系统优化"}])
    assert plan.intent in ("analysis", "general")
    assert plan.complexity == "high"
    assert "下一步建议" in plan.answer_contract
    assert plan.retrieval_k == 8


def test_build_qa_plan_clarification_triggered_for_vague_request():
    from app.services.agent.qa_orchestrator import build_qa_plan

    plan = build_qa_plan("帮我优化一下", [])
    assert plan.should_clarify is True
    assert len(plan.clarify_question) > 0
    assert len(plan.clarify_suggestions or []) >= 1


def test_build_qa_plan_database_clarify_has_targeted_suggestions():
    from app.services.agent.qa_orchestrator import build_qa_plan

    plan = build_qa_plan("帮我查一下数据库", [])
    assert plan.should_clarify is True
    joined = " ".join(plan.clarify_suggestions or [])
    assert "表" in joined or "字段" in joined


def test_build_qa_plan_followup_should_not_over_clarify():
    from app.services.agent.qa_orchestrator import build_qa_plan

    history = [
        {"role": "user", "content": "请帮我优化数据库查询慢的问题"},
        {"role": "assistant", "content": "可以，从索引和执行计划两方面优化"},
    ]
    plan = build_qa_plan("那下一步呢", history)
    assert plan.should_clarify is False


def test_build_qa_plan_has_intent_confidence():
    from app.services.agent.qa_orchestrator import build_qa_plan

    plan = build_qa_plan("请分析这个架构 trade-off", [])
    assert 0.0 <= plan.intent_confidence <= 1.0
    assert plan.intent_confidence >= 0.72

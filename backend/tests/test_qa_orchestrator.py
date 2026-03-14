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

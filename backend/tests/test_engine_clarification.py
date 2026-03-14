import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_detect_clarification_for_explicit_question():
    from app.services.agent.engine import _detect_clarification
    from app.services.agent.output_parser import ParsedOutput

    parsed = ParsedOutput(type="final", content="请提供你的数据库表名和筛选条件？")
    result = _detect_clarification(parsed.content, parsed)
    assert result is not None
    assert "请提供" in result["question"]


def test_detect_clarification_avoids_false_positive_for_long_answer_with_tail_question():
    from app.services.agent.engine import _detect_clarification
    from app.services.agent.output_parser import ParsedOutput

    content = (
        "结论：先做索引优化，再做查询改写。\n"
        "原因：当前慢查询主要来自全表扫描和排序回表。\n"
        "步骤：1) 添加联合索引；2) 用覆盖索引改写查询；3) 用执行计划验证。\n"
        "这样是否更清晰？"
    )
    parsed = ParsedOutput(type="final", content=content)
    result = _detect_clarification(content, parsed)
    assert result is None

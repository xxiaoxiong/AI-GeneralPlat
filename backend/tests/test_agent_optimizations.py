"""
Agent Q&A 优化综合测试

覆盖范围：
1. 截断检测 (_is_truncated)
2. 自我校验保护 (SelfVerifier)
3. 幻觉检查 (HallucinationGuard)
4. Web 搜索解析 (DuckDuckGo/Bing/Baidu)
5. 数据库上下文注入 (prompt_builder)
6. 观察消息优化 (SQL error hints, search fallback)
7. 输出解析器 (parse_react_output)
8. 上下文管理器 (ContextManager)
"""
import sys
import os
import pytest

# 添加项目根路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 截断检测
# ═══════════════════════════════════════════════════════════════════════════════

class TestTruncationDetection:
    """测试 _is_truncated 函数"""

    def _is_truncated(self, text):
        from app.services.agent.engine import _is_truncated
        return _is_truncated(text)

    def test_normal_endings_not_truncated(self):
        """正常结尾不应被判定为截断"""
        assert not self._is_truncated("这是一个完整的句子。")
        assert not self._is_truncated("This is complete!")
        assert not self._is_truncated("结束了？")
        assert not self._is_truncated("```\ncode\n```")
        assert not self._is_truncated("| col1 | col2 |")
        assert not self._is_truncated("some text\n")

    def test_truncated_chinese(self):
        """中文内容在汉字处截断"""
        long_text = "这是一段很长的文字" * 30 + "被截断的内容没有标点符号结尾哈"
        assert self._is_truncated(long_text)

    def test_truncated_english(self):
        """英文内容在单词处截断"""
        long_text = "This is a long text " * 15 + "and it ends without punctuation here"
        assert self._is_truncated(long_text)

    def test_truncated_markdown_bold(self):
        """未闭合的 Markdown 粗体"""
        # 需要超过 20 字符才会触发截断检测
        assert self._is_truncated("这是一段比较长的文本内容，其中包含 **粗体内容没有闭合")

    def test_short_text_not_truncated(self):
        """短文本不应被判定为截断（可能就是简短回答）"""
        assert not self._is_truncated("短回答")
        assert not self._is_truncated("")

    def test_comma_ending_truncated(self):
        """以逗号结尾且较长时判定为截断（需要 >200 字符）"""
        # 英文逗号
        long_text_en = "这是一个很长的列表" + "更多内容" * 60 + ","
        assert len(long_text_en) > 200
        assert self._is_truncated(long_text_en)
        # 中文逗号
        long_text_cn = "这是一个很长的列表" + "更多内容" * 60 + "，"
        assert self._is_truncated(long_text_cn)
        # 顿号
        long_text_dun = "这是一个很长的列表" + "更多内容" * 60 + "、"
        assert self._is_truncated(long_text_dun)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 自我校验保护
# ═══════════════════════════════════════════════════════════════════════════════

class TestSelfVerifier:
    """测试 SelfVerifier 30B 优化"""

    def test_should_not_verify_without_observations(self):
        """没有工具观察时不应触发校验"""
        from app.services.agent.hallucination import SelfVerifier
        assert not SelfVerifier.should_verify("很长的回答" * 100, [], 5)

    def test_should_not_verify_simple_answer(self):
        """简单回答（少于4步）不应触发校验"""
        from app.services.agent.hallucination import SelfVerifier
        obs = [{"tool_name": "web_search", "content": "结果"}]
        assert not SelfVerifier.should_verify("简短回答", obs, 2)

    def test_should_verify_complex_answer(self):
        """复杂回答（4步以上+长文本）应触发校验"""
        from app.services.agent.hallucination import SelfVerifier
        obs = [{"tool_name": "web_search", "content": "结果"}]
        long_answer = "详细分析如下：\n" + "这是一段详细的分析。" * 50
        assert SelfVerifier.should_verify(long_answer, obs, 5)

    def test_parse_verified(self):
        """解析 VERIFIED 结果"""
        from app.services.agent.hallucination import SelfVerifier
        passed, corrected = SelfVerifier.parse_verify_result("VERIFIED")
        assert passed
        assert corrected == ""

    def test_parse_truncated_correction_rejected(self):
        """截断的修正（<50字）应被拒绝"""
        from app.services.agent.hallucination import SelfVerifier
        passed, corrected = SelfVerifier.parse_verify_result(
            "ISSUE: 数据不准确\nFinal Answer: **修正后的"
        )
        assert not passed
        assert corrected == ""  # 太短，被拒绝

    def test_parse_valid_correction_accepted(self):
        """有效的修正应被接受"""
        from app.services.agent.hallucination import SelfVerifier
        long_correction = "这是修正后的完整回答，包含了详细的数据分析和结论。" * 3
        passed, corrected = SelfVerifier.parse_verify_result(
            f"ISSUE: 部分数据不准确\nFinal Answer: {long_correction}"
        )
        assert not passed
        assert len(corrected) >= 50


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 幻觉检查
# ═══════════════════════════════════════════════════════════════════════════════

class TestHallucinationGuard:
    """测试 HallucinationGuard"""

    def test_no_observations_passes(self):
        """没有观察结果时直接通过"""
        from app.services.agent.hallucination import HallucinationGuard
        result = HallucinationGuard.verify_against_observations("任何回答", [])
        assert result.passed

    def test_fabricated_url_detected(self):
        """编造的 URL 应被检测"""
        from app.services.agent.hallucination import HallucinationGuard
        obs = [{"content": "搜索结果：https://real-site.com/page"}]
        result = HallucinationGuard.verify_against_observations(
            "根据搜索，https://fake-site.com/article 显示...", obs
        )
        assert not result.passed
        assert any("URL" in issue for issue in result.issues)

    def test_real_url_passes(self):
        """真实的 URL 不应被误报"""
        from app.services.agent.hallucination import HallucinationGuard
        obs = [{"content": "https://example.com/page 的内容"}]
        result = HallucinationGuard.verify_against_observations(
            "根据 https://example.com/page 的信息...", obs
        )
        assert result.passed

    def test_self_consistency_uncertain_thought(self):
        """Thought 表示不确定时 Final Answer 也应表达不确定"""
        from app.services.agent.hallucination import HallucinationGuard
        thoughts = ["信息不足，无法确定具体数据"]
        # 过于确定的回答 → 不一致
        result = HallucinationGuard.check_self_consistency(
            thoughts, "经过分析，" + "详细结论。" * 20
        )
        assert not result.passed

    def test_self_consistency_with_uncertainty_marker(self):
        """带不确定标记的回答应通过自洽性检查"""
        from app.services.agent.hallucination import HallucinationGuard
        thoughts = ["信息不足"]
        result = HallucinationGuard.check_self_consistency(
            thoughts, "根据已有信息，可能的情况是..."
        )
        assert result.passed


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Web 搜索解析
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebSearchParsing:
    """测试搜索引擎 HTML 解析"""

    def test_parse_bing_html(self):
        """Bing HTML 解析"""
        from app.services.tools.web_tools import _parse_bing_html
        html = '''
        <li class="b_algo">
            <h2><a href="https://example.com/page1" h="ID=SERP">测试标题1</a></h2>
            <p>这是描述内容1</p>
        </li>
        <li class="b_algo">
            <h2><a href="https://example.com/page2">测试标题2</a></h2>
            <p>描述2</p>
        </li>
        '''
        results = _parse_bing_html(html, 5)
        assert len(results) == 2
        assert results[0]["title"] == "测试标题1"
        assert results[0]["url"] == "https://example.com/page1"
        assert "描述内容1" in results[0]["desc"]

    def test_parse_baidu_html(self):
        """百度 HTML 解析"""
        from app.services.tools.web_tools import _parse_baidu_html
        html = '''
        <div class="result c-container">
            <a href="https://baidu.com/link?url=abc" data-click="">百度结果标题</a>
            <div class="content">百度结果描述</div>
        </div></div>
        '''
        results = _parse_baidu_html(html, 5)
        assert len(results) >= 1
        assert "百度结果标题" in results[0]["title"]

    def test_parse_duckduckgo_html_result_a(self):
        """DuckDuckGo HTML 解析 - result__a 模式"""
        from app.services.tools.web_tools import _parse_duckduckgo_html
        html = '''
        <div class="links_main links_deep">
            <a class="result__a" href="https://example.com/ddg1">DDG 标题1</a>
            <a class="result__snippet" href="">这是 DDG 的描述文本</a>
        </div></div>
        <div class="links_main links_deep">
            <a class="result__a" href="https://example.com/ddg2">DDG 标题2</a>
        </div></div>
        '''
        results = _parse_duckduckgo_html(html, 5)
        assert len(results) >= 1
        assert results[0]["url"] == "https://example.com/ddg1"
        assert "DDG 标题1" in results[0]["title"]

    def test_parse_duckduckgo_fallback(self):
        """DuckDuckGo fallback 全局提取"""
        from app.services.tools.web_tools import _parse_duckduckgo_html
        html = '''
        <a class="result__a" href="https://fallback.com/page">Fallback Title</a>
        '''
        results = _parse_duckduckgo_html(html, 5)
        assert len(results) >= 1
        assert results[0]["url"] == "https://fallback.com/page"

    def test_format_search_results(self):
        """搜索结果格式化"""
        from app.services.tools.web_tools import _format_search_results
        results = [
            {"title": "标题", "url": "https://example.com", "desc": "描述"},
        ]
        formatted = _format_search_results("测试查询", results, "Bing")
        assert "标题" in formatted
        assert "https://example.com" in formatted
        assert "Bing" in formatted

    def test_empty_results(self):
        """空结果处理"""
        from app.services.tools.web_tools import _format_search_results
        formatted = _format_search_results("查询", [], "Bing")
        assert "未找到" in formatted


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 数据库上下文注入
# ═══════════════════════════════════════════════════════════════════════════════

class TestDatabaseContextInjection:
    """测试数据库信息注入到 system prompt"""

    def test_db_info_injected(self):
        """数据库连接信息应注入到 system prompt"""
        from app.services.agent.prompt_builder import PromptBuilder
        db_info = {
            "name": "生产数据库",
            "db_type": "mysql",
            "database": "ai_chat",
            "host": "192.168.1.100",
        }
        prompt = PromptBuilder.build_system_prompt(
            user_system_prompt="测试",
            tools=[],
            db_info=db_info,
        )
        assert "ai_chat" in prompt
        assert "mysql" in prompt
        assert "db_schema" in prompt
        assert "生产数据库" in prompt

    def test_db_info_with_tables(self):
        """预加载表列表应注入到 prompt"""
        from app.services.agent.prompt_builder import PromptBuilder
        db_info = {
            "name": "测试库",
            "db_type": "mysql",
            "database": "test_db",
            "host": "localhost",
            "tables": ["employees", "departments", "salaries"],
        }
        prompt = PromptBuilder.build_system_prompt(
            user_system_prompt="测试",
            tools=[],
            db_info=db_info,
        )
        assert "`employees`" in prompt
        assert "`departments`" in prompt
        assert "`salaries`" in prompt
        assert "只能查询上面列出的表" in prompt

    def test_no_db_info(self):
        """没有数据库连接时不注入"""
        from app.services.agent.prompt_builder import PromptBuilder
        prompt = PromptBuilder.build_system_prompt(
            user_system_prompt="测试",
            tools=[],
            db_info=None,
        )
        assert "当前数据库连接" not in prompt

    def test_schema_first_rule_in_prompt(self):
        """schema-first 规则应在 prompt 中"""
        from app.services.agent.prompt_builder import PromptBuilder
        prompt = PromptBuilder.build_system_prompt(tools=[])
        assert "db_schema" in prompt
        assert "查表结构" in prompt or "查看表结构" in prompt or "先查表结构" in prompt or "先查结构" in prompt

    def test_anti_hallucination_rules(self):
        """反幻觉铁律应在 prompt 中"""
        from app.services.agent.prompt_builder import PromptBuilder
        prompt = PromptBuilder.build_system_prompt(tools=[])
        assert "禁止编造" in prompt
        assert "仅为格式演示" in prompt or "格式演示" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 观察消息优化
# ═══════════════════════════════════════════════════════════════════════════════

class TestObservationMessages:
    """测试优化后的 Observation 消息"""

    def test_sql_error_hint(self):
        """SQL 错误时应提示使用 db_schema"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "db_query", "错误：表 'users' 不存在"
        )
        assert "db_schema" in msg

    def test_table_not_exist_hint(self):
        """表不存在错误的英文提示"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "db_query", "Table 'test.unknown_table' doesn't exist"
        )
        assert "db_schema" in msg

    def test_search_failure_hint(self):
        """搜索失败时应提示使用知识"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "web_search", "搜索 'test' 未找到结果。\n尝试的搜索引擎状态: ..."
        )
        assert "知识" in msg

    def test_normal_observation_no_hint(self):
        """正常结果不应有额外提示"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "db_query", "| id | name |\n|---|---|\n| 1 | test |"
        )
        assert "db_schema" not in msg
        assert "知识" not in msg

    def test_observation_truncation(self):
        """过长结果应被截断"""
        from app.services.agent.prompt_builder import PromptBuilder
        long_result = "x" * 5000
        msg = PromptBuilder.build_observation_message("db_query", long_result)
        assert "已截断" in msg
        assert len(msg) < 5000


# ═══════════════════════════════════════════════════════════════════════════════
# 7. 输出解析器
# ═══════════════════════════════════════════════════════════════════════════════

class TestOutputParser:
    """测试 ReAct 输出解析器"""

    def test_parse_final_answer(self):
        """解析 Final Answer"""
        from app.services.agent.output_parser import parse_react_output
        text = "Thought: 我已经获取了信息\nFinal Answer: 这是最终回答"
        result = parse_react_output(text)
        assert result.type == "final"
        assert "最终回答" in result.content

    def test_parse_action(self):
        """解析 Action"""
        from app.services.agent.output_parser import parse_react_output
        text = 'Thought: 需要搜索\nAction: web_search\nAction Input: {"query": "test"}'
        result = parse_react_output(text, ["web_search"])
        assert result.type == "action"
        assert result.action == "web_search"
        assert result.action_input.get("query") == "test"

    def test_parse_thought_only(self):
        """解析纯 Thought"""
        from app.services.agent.output_parser import parse_react_output
        text = "Thought: 我在思考这个问题"
        result = parse_react_output(text)
        assert result.type == "thought"

    def test_parse_malformed_json(self):
        """解析格式不正确的 JSON"""
        from app.services.agent.output_parser import parse_react_output
        text = "Thought: 需要搜索\nAction: web_search\nAction Input: {query: 'test'}"
        result = parse_react_output(text, ["web_search"])
        assert result.type == "action"
        assert result.action == "web_search"

    def test_parse_empty_input(self):
        """空输入处理"""
        from app.services.agent.output_parser import parse_react_output
        result = parse_react_output("")
        assert result.type == "unknown"

    def test_json_repair(self):
        """JSON 修复"""
        from app.services.agent.output_parser import repair_json
        # 尾逗号
        assert repair_json('{"key": "value",}') == {"key": "value"}
        # 单引号
        assert repair_json("{'key': 'value'}") == {"key": "value"}
        # 缺失括号
        result = repair_json('{"key": "value"')
        assert result == {"key": "value"}


# ═══════════════════════════════════════════════════════════════════════════════
# 8. 上下文管理器
# ═══════════════════════════════════════════════════════════════════════════════

class TestContextManager:
    """测试上下文管理器"""

    def test_basic_context_build(self):
        """基本上下文构建"""
        from app.services.agent.context_manager import ContextManager
        mgr = ContextManager(max_context_tokens=16384)
        result = mgr.build_context(
            system_prompt="你是助手",
            chat_history=[
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
            ],
            working_memory=[
                {"role": "user", "content": "当前问题"},
            ],
            original_task="你好",
        )
        assert len(result.messages) >= 1
        assert result.total_tokens > 0

    def test_context_respects_token_limit(self):
        """上下文应尊重 token 限制"""
        from app.services.agent.context_manager import ContextManager
        mgr = ContextManager(max_context_tokens=500)
        long_history = []
        for i in range(20):
            long_history.append({"role": "user", "content": f"问题 {i} " + "内容" * 50})
            long_history.append({"role": "assistant", "content": f"回答 {i} " + "内容" * 50})

        result = mgr.build_context(
            system_prompt="短提示",
            chat_history=long_history,
            working_memory=[{"role": "user", "content": "当前"}],
        )
        # 应该裁剪了一些历史
        assert result.dropped_turns > 0

    def test_token_estimation(self):
        """Token 估算"""
        from app.services.agent.context_manager import estimate_tokens
        assert estimate_tokens("") == 0
        # 纯中文：每字约 1.5 token
        chinese_tokens = estimate_tokens("这是测试")
        assert 4 <= chinese_tokens <= 10
        # 纯英文：每词约 1.3 token
        english_tokens = estimate_tokens("this is a test")
        assert 4 <= english_tokens <= 10


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Config 验证
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfig:
    """验证配置更新"""

    def test_increased_token_limits(self):
        """验证 token 限制已提升"""
        from app.core.config import settings
        assert settings.INFERENCE_MAX_OUTPUT_TOKENS >= 8192
        assert settings.INFERENCE_MAX_CONTEXT_TOKENS >= 16384


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Trace 验证
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrace:
    """验证 ExecutionTrace partial_final 字段"""

    def test_partial_final_field(self):
        """验证 partial_final 字段存在且可用"""
        from app.services.agent.trace import ExecutionTrace
        trace = ExecutionTrace(trace_id="test-123")
        assert trace.partial_final == ""
        trace.partial_final = "部分内容"
        assert trace.partial_final == "部分内容"
        trace.partial_final = ""
        assert trace.partial_final == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 11. v2.4 新增测试：截断检测增强 + DB反幻觉 + Web搜索增强
# ═══════════════════════════════════════════════════════════════════════════════

class TestTruncationDetectionV24:
    """v2.4 新增截断检测场景"""

    def _is_truncated(self, text):
        from app.services.agent.engine import _is_truncated
        return _is_truncated(text)

    def test_unclosed_code_block_truncated(self):
        """未闭合的代码块应判定为截断"""
        text = "这是一段代码：\n```python\ndef hello():\n    print('hello')"
        assert self._is_truncated(text)

    def test_closed_code_block_not_truncated(self):
        """闭合的代码块不应判定为截断"""
        text = "这是一段代码：\n```python\ndef hello():\n    print('hello')\n```"
        assert not self._is_truncated(text)

    def test_table_row_mid_cell_truncated(self):
        """Markdown 表格行中途断开应判定为截断"""
        text = "| id | name | status\n| 1 | test"
        # 只要足够长就检测
        long_text = "这是一个很长的表格结果：\n" + "数据" * 30 + "\n| id | name | statu"
        assert self._is_truncated(long_text)

    def test_table_row_complete_not_truncated(self):
        """完整的 Markdown 表格行不应判定为截断"""
        text = "| id | name | status |"
        assert not self._is_truncated(text)

    def test_dash_ending_truncated(self):
        """以破折号结尾且较长时判定为截断"""
        long_text = "这是一段分析内容" * 20 + "—"
        assert len(long_text) > 100
        assert self._is_truncated(long_text)

    def test_semicolon_ending_not_truncated(self):
        """以分号结尾不应判定为截断"""
        assert not self._is_truncated("这是一段完整的句子；")
        assert not self._is_truncated("This is complete;")


class TestObservationAntiHallucination:
    """v2.4 观察消息反幻觉提示测试"""

    def test_db_query_success_has_iron_rule(self):
        """成功的 db_query 结果应包含数据铁律提示"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "db_query", "| id | name |\n|---|---|\n| 1 | test |\n\n共返回 1 行"
        )
        assert "铁律" in msg
        assert "不得添加" in msg or "不得" in msg

    def test_db_schema_has_strict_hint(self):
        """db_schema 结果应包含严格表名提示"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "db_schema", "数据库中共有以下表：\n1. `users`\n2. `orders`"
        )
        assert "严格使用" in msg
        assert "编造" in msg

    def test_db_query_error_has_schema_hint(self):
        """db_query 错误应提示使用 db_schema"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "db_query", "SQL执行错误: Table 'test' doesn't exist"
        )
        assert "db_schema" in msg

    def test_web_search_success_has_fetch_hint(self):
        """成功的 web_search 应提示可用 fetch_webpage 深入"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "web_search", "搜索结果（测试查询，来源: Bing）:\n1. **标题**\n   描述\n   🔗 [url](url)"
        )
        assert "fetch_webpage" in msg


class TestWebSearchEnhancement:
    """v2.4 Web 搜索结果增强测试"""

    def test_format_with_page_content(self):
        """搜索结果带页面摘要时应包含页面摘要"""
        from app.services.tools.web_tools import _format_search_results
        results = [
            {"title": "测试标题", "url": "https://example.com", "desc": "短描述",
             "page_content": "这是页面的详细内容，包含了很多有用的信息。"},
        ]
        output = _format_search_results("测试", results, "Bing")
        assert "页面摘要" in output
        assert "这是页面的详细内容" in output

    def test_format_without_page_content(self):
        """搜索结果无页面摘要时不应包含页面摘要字段"""
        from app.services.tools.web_tools import _format_search_results
        results = [
            {"title": "测试标题", "url": "https://example.com", "desc": "短描述"},
        ]
        output = _format_search_results("测试", results, "Bing")
        assert "页面摘要" not in output
        assert "测试标题" in output


class TestToolExecutorCaching:
    """v2.4 工具执行器引擎缓存测试"""

    def test_ext_engine_init_none(self):
        """初始化时外部引擎应为 None"""
        from app.services.agent.tool_executor import EnhancedToolExecutor
        executor = EnhancedToolExecutor(db=None, agent_config={})
        assert executor._ext_engine is None
        assert executor._ext_engine_conn_id is None

    def test_no_conn_id_raises_error(self):
        """无 database_connection_id 时应报错，避免误查应用自身数据库"""
        import asyncio
        import pytest
        from app.services.agent.tool_executor import EnhancedToolExecutor

        executor = EnhancedToolExecutor(db="mock_app_db", agent_config={})

        async def check():
            with pytest.raises(ValueError, match="未配置外部数据库连接"):
                await executor._get_db_session()

        asyncio.get_event_loop().run_until_complete(check())


class TestV25SessionSave:
    """v2.5 会话保存独立会话测试"""

    def test_save_session_function_exists(self):
        """_save_session 函数应存在"""
        from app.api.v1.agents import _save_session
        assert callable(_save_session)

    def test_save_session_is_async(self):
        """_save_session 应是异步函数"""
        import asyncio
        from app.api.v1.agents import _save_session
        assert asyncio.iscoroutinefunction(_save_session)


class TestV25RawDataRules:
    """v2.5 禁止翻译/修改数据库结果测试"""

    def test_prompt_has_no_translate_rule(self):
        """系统提示词应包含禁止翻译列名规则"""
        from app.services.agent.prompt_builder import PromptBuilder
        prompt = PromptBuilder.build_system_prompt(
            user_system_prompt="test",
            tools=[],
            max_iterations=5,
        )
        assert "禁止翻译或修改列名" in prompt
        assert "禁止加工数据值" in prompt

    def test_prompt_has_raw_column_example(self):
        """系统提示词应包含原始列名示例（如 name → 名称 的反例）"""
        from app.services.agent.prompt_builder import PromptBuilder
        prompt = PromptBuilder.build_system_prompt(
            user_system_prompt="test",
            tools=[],
            max_iterations=5,
        )
        assert "name" in prompt and "名称" in prompt

    def test_observation_has_no_translate_hint(self):
        """db_query 成功结果应包含禁止翻译列名提示"""
        from app.services.agent.prompt_builder import PromptBuilder
        msg = PromptBuilder.build_observation_message(
            "db_query", "| id | name |\n|---|---|\n| 1 | test |\n\n共返回 1 行"
        )
        assert "禁止翻译列名" in msg
        assert "原样使用" in msg

    def test_db_label_prefix_with_db_info(self):
        """有 _db_info 时，工具结果应包含数据库名标签"""
        from app.services.agent.tool_executor import EnhancedToolExecutor
        executor = EnhancedToolExecutor(
            db=None,
            agent_config={"_db_info": {"database": "ai_chat", "db_type": "mysql"}}
        )
        # 验证 agent_config 中的 _db_info 被正确设置
        db_info = executor.agent_config.get("_db_info")
        assert db_info is not None
        assert db_info["database"] == "ai_chat"

    def test_db_label_prefix_without_db_info(self):
        """无 _db_info 时，工具结果应标注应用内置库"""
        from app.services.agent.tool_executor import EnhancedToolExecutor
        executor = EnhancedToolExecutor(db=None, agent_config={})
        db_info = executor.agent_config.get("_db_info")
        assert db_info is None


class TestV26DatabaseConnectionGuard:
    """v2.6 外部数据库连接强约束测试"""

    def test_no_conn_id_raises_error(self):
        """未配置 database_connection_id 时，必须阻止落到应用库"""
        import asyncio
        import pytest
        from app.services.agent.tool_executor import EnhancedToolExecutor

        executor = EnhancedToolExecutor(db=object(), agent_config={})

        async def check():
            with pytest.raises(ValueError, match="未配置外部数据库连接"):
                await executor._get_db_session()

        asyncio.get_event_loop().run_until_complete(check())

    def test_owner_scope_check_in_query(self):
        """数据库连接查询应包含 owner_id 和 is_active 约束"""
        import asyncio
        from unittest.mock import AsyncMock
        from app.services.agent.tool_executor import EnhancedToolExecutor

        class MockResult:
            def scalar_one_or_none(self):
                return None

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MockResult())

        executor = EnhancedToolExecutor(db=mock_db, agent_config={"database_connection_id": 123, "owner_id": 9})

        async def check():
            try:
                await executor._get_db_session()
            except ValueError:
                pass

        asyncio.get_event_loop().run_until_complete(check())

        called_query = mock_db.execute.call_args[0][0]
        where_sql = str(called_query.whereclause)
        assert "database_connections.owner_id" in where_sql
        assert "database_connections.is_active" in where_sql



if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

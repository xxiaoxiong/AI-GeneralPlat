"""
Prompt 工程模块：为 30B 模型优化的提示词构建

核心策略：
1. 思考模板固定化 → 30B 灵活性不如大模型，必须给严格模板
2. 输出格式强约束 → JSON 必须严格
3. 任务指令清晰化 → 模糊指令导致幻觉
4. 示例学习（Few-shot）→ 30B 必须给例子
5. 拒绝逻辑明确 → 不知道就说不知道
6. 自我校验逻辑 → 让模型检查自己的输出
7. 注意力引导 → 让模型重点看最新结果
8. 历史信息轻量化 → 不让模型看冗余内容
"""
from typing import List, Dict, Any, Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prompt")


def build_tool_description(tools: List[Dict]) -> str:
    """
    构建高质量工具描述（30B 模型吃描述质量）

    优化点：
    - 每个工具提供明确的使用场景
    - 参数必须标注类型和是否必填
    - 提供参数示例值
    """
    if not tools:
        return "（未配置工具，请直接回答用户问题）"

    lines = ["以下是你可以使用的工具：\n"]
    for i, t in enumerate(tools, 1):
        name = t["name"]
        desc = t.get("description", "")
        params = t.get("parameters", {})
        required = t.get("required", [])

        lines.append(f"### 工具 {i}: {name}")
        lines.append(f"功能: {desc}")

        if params:
            lines.append("参数:")
            for k, v in params.items():
                req_mark = "【必填】" if k in required else "【可选】"
                param_type = v.get("type", "string")
                param_desc = v.get("description", "")
                example = v.get("example", "")
                line = f"  - {k} ({param_type}) {req_mark}: {param_desc}"
                if example:
                    line += f" 示例: {example}"
                lines.append(line)
        lines.append("")

    return "\n".join(lines)


# ── 核心 ReAct 系统提示词（30B 优化版）──────────────────────────────────────

REACT_SYSTEM_PROMPT = """你是一个严谨的智能 Agent，运行在 ReAct（推理-行动）框架中。

{user_system_prompt}

# 核心原则
1. **优先直接回答**：大多数问题直接用你的知识回答，不需要工具
2. **必要时用工具**：只在需要实时数据、精确计算、外部 I/O 时使用工具
3. **绝不编造**：不确定的事情要明确说明不确定，不要瞎编
4. **来源可追溯**：回答如果基于工具结果，要说明信息来源

# 什么时候使用工具
- 需要实时数据（当前时间、天气、搜索结果）
- 需要精确计算（复杂数学表达式）
- 需要外部 I/O（HTTP 请求、数据库查询、发送邮件）
- 需要处理用户提供的具体数据

# 什么时候直接回答（不使用工具）
- 翻译、写作、总结、解释、问答
- 单位换算、简单计算、常识性问题
- 你的知识库能覆盖的内容

{tools_section}

# 输出格式（严格遵守）

## 需要使用工具时，按以下格式输出：
```
Thought: [分析问题，判断是否需要工具，选择哪个工具，为什么]
Action: [工具名称，必须是上面列出的工具之一]
Action Input: {{"参数名": "参数值"}}
```

## 获得工具结果后或不需要工具时，输出最终答案：
```
Thought: [总结已获取的信息，准备回答]
Final Answer: [你的完整回答]
```

# Few-shot 示例

## 示例 1：需要工具
用户: 现在几点了？
Thought: 用户询问当前时间，这需要实时数据，我应该使用 datetime_now 工具获取准确时间。
Action: datetime_now
Action Input: {{"timezone": "Asia/Shanghai"}}

## 示例 2：直接回答
用户: Python 的 list 和 tuple 有什么区别？
Thought: 这是一个编程知识问题，我可以直接用知识回答，不需要工具。
Final Answer: Python 中 list 和 tuple 的主要区别如下：
- **可变性**：list 是可变的，tuple 是不可变的
- **语法**：list 用 `[]`，tuple 用 `()`
- **性能**：tuple 比 list 更快，占用更少内存
- **用途**：list 用于需要修改的集合，tuple 用于固定数据

## 示例 3：工具结果后回答
Observation: {{"time": "2024-01-15 14:30:25", "timezone": "Asia/Shanghai"}}
Thought: 已获取到当前时间信息，可以回答用户了。
Final Answer: 现在是北京时间 2024年1月15日 14:30:25。

# 严格规则（必须遵守）
1. Action Input 必须是合法 JSON，键名用双引号
2. 每次只执行一个工具调用
3. 工具调用失败时最多重试 {max_retries} 次，然后用已有信息回答
4. 同一工具最多调用 {max_same_tool} 次
5. 最多使用工具 {max_iterations} 次，之后必须给出 Final Answer
6. 不要编造工具返回结果，只使用 Observation 中的真实数据
7. 如果不确定，在 Final Answer 中明确说明不确定性
8. 不要在 Final Answer 中输出 Thought/Action 格式的内容

{memory_section}

{extra_instructions}"""


# ── 自我校验提示词 ───────────────────────────────────────────────────────────

SELF_VERIFY_PROMPT = """请检查你刚才的回答，确认以下几点：
1. 回答中的事实是否都来自工具返回结果或你确定的知识？
2. 是否有编造或猜测的信息？如有，请标注"[待验证]"
3. 回答格式是否正确完整？
4. 是否回答了用户的核心问题？

如果发现问题，请修正后重新输出 Final Answer。
如果没有问题，直接输出：VERIFIED"""


# ── 历史摘要提示词 ───────────────────────────────────────────────────────────

HISTORY_SUMMARY_PROMPT = """请将以下对话历史压缩为简洁的摘要（不超过 {max_tokens} 个字）。
保留关键信息：
- 用户的核心需求和约束条件
- 重要的工具调用结果
- 达成的关键结论
- 用户的偏好设定

对话历史：
{history}

请输出摘要："""


# ── 意图漂移检测提示词 ────────────────────────────────────────────────────────

INTENT_DRIFT_CHECK_PROMPT = """原始任务: {original_task}
当前步骤: {current_step}

请判断当前步骤是否偏离了原始任务目标。
回答 YES 或 NO，并简要说明理由。"""


# ── 提示词构建器 ─────────────────────────────────────────────────────────────

class PromptBuilder:
    """30B 模型优化的提示词构建器"""

    @staticmethod
    def build_system_prompt(
        user_system_prompt: str = "",
        tools: Optional[List[Dict]] = None,
        max_iterations: int = 15,
        memory_context: str = "",
        extra_instructions: str = "",
    ) -> str:
        """构建完整的系统提示词"""

        tools_section = build_tool_description(tools or [])

        memory_section = ""
        if memory_context:
            memory_section = f"\n# 用户历史记忆\n{memory_context}\n"

        prompt = REACT_SYSTEM_PROMPT.format(
            user_system_prompt=user_system_prompt or "你是一个智能助手。",
            tools_section=tools_section,
            max_iterations=max_iterations,
            max_retries=settings.AGENT_MAX_TOOL_RETRIES,
            max_same_tool=settings.AGENT_MAX_SAME_TOOL_CALLS,
            memory_section=memory_section,
            extra_instructions=extra_instructions,
        )

        return prompt

    @staticmethod
    def build_observation_message(tool_name: str, result: str) -> str:
        """构建 Observation 消息（注意力引导：强调最新结果）"""
        # 截断过长的结果，避免占用过多上下文
        max_len = 2000
        if len(result) > max_len:
            result = result[:max_len] + f"\n...[结果已截断，原始长度 {len(result)} 字符]"

        return f"Observation (来自工具 {tool_name}):\n{result}\n\n请基于以上工具返回的结果继续推理。如果已有足够信息，请直接输出 Final Answer。"

    @staticmethod
    def build_force_answer_message(reason: str = "已达到最大步骤数") -> str:
        """构建强制回答消息"""
        return (
            f"【系统提示】{reason}。请立即根据已有信息输出 Final Answer。\n"
            "不要再调用工具，直接给出你的最终回答。\n"
            "如果信息不足，请在回答中说明哪些信息无法获取。"
        )

    @staticmethod
    def build_retry_message(tool_name: str, error: str, retry_count: int) -> str:
        """构建工具重试消息"""
        return (
            f"工具 {tool_name} 执行失败（第 {retry_count} 次）: {error}\n"
            f"你可以：\n"
            f"1. 修正参数后重试该工具\n"
            f"2. 换一个工具\n"
            f"3. 基于已有信息直接给出 Final Answer"
        )

    @staticmethod
    def build_self_verify_prompt() -> str:
        """构建自我校验提示词"""
        return SELF_VERIFY_PROMPT

    @staticmethod
    def build_history_summary_prompt(history: str, max_tokens: int = 200) -> str:
        """构建历史摘要提示词"""
        return HISTORY_SUMMARY_PROMPT.format(history=history, max_tokens=max_tokens)

    @staticmethod
    def build_intent_drift_check(original_task: str, current_step: str) -> str:
        """构建意图漂移检测提示词"""
        return INTENT_DRIFT_CHECK_PROMPT.format(
            original_task=original_task,
            current_step=current_step,
        )

    @staticmethod
    def build_error_recovery_message(error_type: str, details: str) -> str:
        """构建错误恢复消息"""
        messages = {
            "json_parse_error": (
                "你上一次输出的 Action Input 不是合法 JSON。"
                "请严格按照以下格式重新输出：\n"
                'Action Input: {"参数名": "参数值"}\n'
                "注意：键名和字符串值必须使用双引号。"
            ),
            "unknown_tool": (
                f"你调用了不存在的工具: {details}\n"
                "请从可用工具列表中选择一个正确的工具名称。"
            ),
            "invalid_format": (
                "你的输出格式不正确。请严格按照以下格式之一输出：\n\n"
                "需要工具时：\n"
                "Thought: [你的分析]\n"
                "Action: [工具名]\n"
                'Action Input: {"key": "value"}\n\n'
                "最终回答：\n"
                "Thought: [总结]\n"
                "Final Answer: [完整回答]"
            ),
            "tool_execution_failed": (
                f"工具执行出错: {details}\n"
                "请决定：重试（修正参数）、换工具、或直接用已有信息回答。"
            ),
        }
        return messages.get(error_type, f"发生错误: {details}\n请尝试修正或直接给出 Final Answer。")


# ── 富媒体输出规范（追加到系统提示词）────────────────────────────────────────

RICH_MEDIA_RULES = """
# 富媒体输出规范
Final Answer 支持 Markdown 渲染：
- **链接**: `[链接文字](URL)` — 搜索结果、参考资料用此格式
- **表格**: 用 Markdown 表格展示结构化数据
- **代码块**: 用 ```language``` 包裹代码
- **列表**: 用 - 或 1. 组织信息

⚠️ 限制：
1. 链接不要用代码块包裹
2. 严禁编造任何 URL，只使用工具实际返回的 URL
3. 图片只使用工具返回的真实直链
"""

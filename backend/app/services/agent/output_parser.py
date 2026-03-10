"""
输出解析器：强鲁棒 ReAct 格式解析 + JSON 自动修复

30B 模型最大弱点之一是输出格式不稳定，本模块提供：
1. 多级容错 ReAct 解析（正则 + 模糊匹配）
2. JSON 自动修复（括号补全、引号修复、尾逗号清理）
3. 格式验证与错误恢复
4. 输出格式强约束检查
"""
import re
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger("parser")


@dataclass
class ParsedOutput:
    """解析后的结构化输出"""
    type: str               # "final" | "action" | "thought" | "unknown"
    content: str = ""       # Final Answer 内容或纯 Thought 内容
    thought: str = ""       # Thought 内容
    action: str = ""        # 工具名称
    action_input: Dict = field(default_factory=dict)  # 工具参数
    raw_text: str = ""      # 原始文本
    confidence: float = 1.0  # 解析置信度 (0~1)
    parse_method: str = ""  # 使用的解析方法

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "content": self.content,
            "thought": self.thought,
            "action": self.action,
            "action_input": self.action_input,
            "confidence": self.confidence,
        }


# ── JSON 修复工具 ────────────────────────────────────────────────────────────

def repair_json(text: str) -> Optional[Dict]:
    """
    多级 JSON 修复策略：
    1. 直接解析
    2. 提取 JSON 子串
    3. 修复常见错误（尾逗号、单引号、缺失括号）
    4. 宽松模式解析
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # 策略 1：直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 策略 2：提取 JSON 子串（在文本中找到 {} 对）
    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    # 策略 3：修复常见错误
    fixed = text
    # 3a. 移除尾逗号
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    # 3b. 单引号替换为双引号（但不替换字符串内的单引号）
    fixed = re.sub(r"(?<![\\])'", '"', fixed)
    # 3c. 修复没有引号的键名
    fixed = re.sub(r'([{,])\s*([a-zA-Z_]\w*)\s*:', r'\1"\2":', fixed)
    # 3d. 补全缺失的右括号
    open_braces = fixed.count('{') - fixed.count('}')
    if open_braces > 0:
        fixed += '}' * open_braces
    open_brackets = fixed.count('[') - fixed.count(']')
    if open_brackets > 0:
        fixed += ']' * open_brackets

    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # 策略 4：尝试从文本中提取键值对构建 JSON
    kv_pattern = re.findall(r'"?(\w+)"?\s*[:：]\s*"([^"]*)"', text)
    if kv_pattern:
        result = {k: v for k, v in kv_pattern}
        return result

    # 策略 5：尝试将整个文本作为单个参数值
    # 这对于 30B 模型输出 Action Input: some_value 的情况有用
    simple_match = re.match(r'^["\']?([^"\'{}]+)["\']?$', text.strip())
    if simple_match:
        return {"input": simple_match.group(1).strip()}

    return None


def validate_json_schema(data: Dict, expected_params: Optional[Dict] = None) -> bool:
    """验证 JSON 是否符合预期的参数 schema"""
    if expected_params is None:
        return True
    for param_name, param_info in expected_params.items():
        if param_info.get("required", False) and param_name not in data:
            return False
    return True


# ── ReAct 输出解析 ───────────────────────────────────────────────────────────

def parse_react_output(text: str, available_tools: Optional[List[str]] = None) -> ParsedOutput:
    """
    多级容错 ReAct 输出解析器

    解析优先级：
    1. 严格格式匹配（Thought/Action/Action Input/Final Answer）
    2. 宽松格式匹配（容忍中文冒号、多余空格等）
    3. 模糊工具名匹配（30B 可能拼错工具名）
    4. 兜底策略（当作 Final Answer 或纯 Thought）
    """
    if not text or not text.strip():
        return ParsedOutput(type="unknown", content="", raw_text=text, confidence=0.0)

    text = text.strip()

    # ── 1. 检测 Final Answer ──────────────────────────────────────────────
    result = _parse_final_answer(text)
    if result:
        return result

    # ── 2. 检测 Action ────────────────────────────────────────────────────
    result = _parse_action(text, available_tools)
    if result:
        return result

    # ── 3. 检测纯 Thought（无 Action 无 Final Answer）────────────────────
    result = _parse_thought_only(text)
    if result:
        return result

    # ── 4. 兜底：检查是否包含工具名（30B 有时不写 Action: 前缀）──────────
    if available_tools:
        result = _parse_implicit_action(text, available_tools)
        if result:
            return result

    # ── 5. 最终兜底：作为 unknown 返回 ────────────────────────────────────
    return ParsedOutput(
        type="unknown",
        content=text,
        raw_text=text,
        confidence=0.3,
        parse_method="fallback",
    )


def _parse_final_answer(text: str) -> Optional[ParsedOutput]:
    """解析 Final Answer"""
    # 严格格式
    patterns = [
        r"Final\s*Answer\s*[:：]\s*([\s\S]+?)$",
        r"最终(?:答案|回答)\s*[:：]\s*([\s\S]+?)$",
        r"(?:FINAL|final)\s*(?:ANSWER|answer)\s*[:：]\s*([\s\S]+?)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            content = match.group(1).strip()
            # 去掉可能残留的结束标记
            content = re.sub(r'\n*(?:Thought|Action|Observation).*$', '', content, flags=re.DOTALL)
            return ParsedOutput(
                type="final",
                content=content,
                raw_text=text,
                confidence=0.95,
                parse_method="regex_final",
            )
    return None


def _parse_action(text: str, available_tools: Optional[List[str]] = None) -> Optional[ParsedOutput]:
    """解析 Action + Action Input"""
    # Thought 提取
    thought = ""
    thought_patterns = [
        r"Thought\s*[:：]\s*(.*?)(?=Action\s*[:：]|$)",
        r"思考\s*[:：]\s*(.*?)(?=Action\s*[:：]|动作\s*[:：]|$)",
    ]
    for tp in thought_patterns:
        tm = re.search(tp, text, re.DOTALL | re.IGNORECASE)
        if tm:
            thought = tm.group(1).strip()
            break

    # Action 提取
    action = ""
    action_patterns = [
        r"Action\s*[:：]\s*(\S+)",
        r"动作\s*[:：]\s*(\S+)",
        r"工具\s*[:：]\s*(\S+)",
    ]
    for ap in action_patterns:
        am = re.search(ap, text, re.IGNORECASE)
        if am:
            action = am.group(1).strip().strip('"\'`')
            break

    if not action:
        return None

    # 模糊匹配工具名（30B 容易拼错）
    if available_tools:
        action = _fuzzy_match_tool(action, available_tools) or action

    # Action Input 提取
    action_input = {}
    input_patterns = [
        r"Action\s*Input\s*[:：]\s*(\{[\s\S]*?\})(?:\s*$|\s*Observation|\s*Thought)",
        r"Action\s*Input\s*[:：]\s*(\{[\s\S]*\})",
        r"参数\s*[:：]\s*(\{[\s\S]*?\})",
        r"输入\s*[:：]\s*(\{[\s\S]*?\})",
    ]

    for ip in input_patterns:
        im = re.search(ip, text, re.IGNORECASE)
        if im:
            raw_json = im.group(1).strip()
            parsed = repair_json(raw_json)
            if parsed is not None:
                action_input = parsed
                break

    # 如果还没解析到参数，尝试提取 Action Input 后面的非 JSON 文本作为单参数
    if not action_input:
        fallback_match = re.search(r"Action\s*Input\s*[:：]\s*(.+?)$", text, re.IGNORECASE | re.MULTILINE)
        if fallback_match:
            raw_val = fallback_match.group(1).strip()
            if raw_val and not raw_val.startswith('{'):
                action_input = {"query": raw_val} if raw_val else {}

    return ParsedOutput(
        type="action",
        thought=thought,
        action=action,
        action_input=action_input,
        raw_text=text,
        confidence=0.9 if action_input else 0.7,
        parse_method="regex_action",
    )


def _parse_thought_only(text: str) -> Optional[ParsedOutput]:
    """解析纯 Thought（没有 Action 和 Final Answer）"""
    patterns = [
        r"Thought\s*[:：]\s*([\s\S]+?)$",
        r"思考\s*[:：]\s*([\s\S]+?)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            content = match.group(1).strip()
            return ParsedOutput(
                type="thought",
                content=content,
                thought=content,
                raw_text=text,
                confidence=0.8,
                parse_method="regex_thought",
            )
    return None


def _parse_implicit_action(text: str, available_tools: List[str]) -> Optional[ParsedOutput]:
    """30B 模型有时不写 Action: 前缀，但提到了工具名"""
    text_lower = text.lower()
    for tool in available_tools:
        if tool.lower() in text_lower:
            # 尝试在工具名附近找到 JSON 参数
            json_match = re.search(r'\{[^{}]*\}', text)
            action_input = {}
            if json_match:
                parsed = repair_json(json_match.group())
                if parsed:
                    action_input = parsed
            return ParsedOutput(
                type="action",
                thought=text.split(tool)[0].strip() if tool in text else "",
                action=tool,
                action_input=action_input,
                raw_text=text,
                confidence=0.5,
                parse_method="implicit_action",
            )
    return None


def _fuzzy_match_tool(name: str, available_tools: List[str]) -> Optional[str]:
    """模糊匹配工具名（处理 30B 模型拼错的情况）"""
    name_lower = name.lower().replace("-", "_").replace(" ", "_")

    # 精确匹配
    for tool in available_tools:
        if tool.lower() == name_lower:
            return tool

    # 前缀匹配
    for tool in available_tools:
        if tool.lower().startswith(name_lower) or name_lower.startswith(tool.lower()):
            return tool

    # 编辑距离容忍 1~2
    for tool in available_tools:
        if _edit_distance(name_lower, tool.lower()) <= 2:
            return tool

    return None


def _edit_distance(s1: str, s2: str) -> int:
    """计算编辑距离"""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[len(s2)]


# ── 输出格式强制校验 ─────────────────────────────────────────────────────────

def validate_output_format(parsed: ParsedOutput) -> List[str]:
    """
    校验解析结果的合法性，返回错误列表
    空列表 = 通过校验
    """
    errors = []

    if parsed.type == "action":
        if not parsed.action:
            errors.append("Action 为空")
        if not isinstance(parsed.action_input, dict):
            errors.append("Action Input 不是有效 JSON 对象")
        # 工具名不应包含特殊字符
        if parsed.action and not re.match(r'^[\w_]+$', parsed.action):
            errors.append(f"工具名包含非法字符: {parsed.action}")

    elif parsed.type == "final":
        if not parsed.content or not parsed.content.strip():
            errors.append("Final Answer 内容为空")

    elif parsed.type == "unknown":
        errors.append("无法解析输出格式")

    return errors

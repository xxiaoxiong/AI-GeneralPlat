"""
Agent ReAct 执行引擎（v1 旧版 - 已由 app.services.agent 包替代）

向后兼容：从新包重新导出 AgentEngine
新代码请使用: from app.services.agent import AgentEngine

工具实现已按分类拆分到 app/services/tools/ 子包：
  datetime_tools.py  📅 时间日期
  math_tools.py      🔢 计算数学
  network_tools.py   🌐 网络请求
  database_tools.py  🗄️ 数据库
  text_tools.py      📝 文本处理
  knowledge_tools.py 🔍 知识检索
  analysis_tools.py  📊 数据分析
  system_tools.py    🛠️ 系统工具
  notify_tools.py    📧 通知推送
"""
# 向后兼容导出
try:
    from app.services.agent import AgentEngine as AgentEngine  # noqa: F401
except ImportError:
    pass  # 回退到下方旧实现
import json
import re
from typing import AsyncGenerator, Dict, Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.services.tools import BUILTIN_TOOLS, ToolExecutor, get_tool_by_name


def tools_to_prompt(tools: List[Dict]) -> str:
    """将工具列表转为提示词中的工具描述"""
    lines = []
    for t in tools:
        params_desc = ", ".join(
            f"{k}({'必填' if k in t.get('required', []) else '可选'}): {v['description']}"
            for k, v in t.get("parameters", {}).items()
        )
        lines.append(f"- {t['name']}: {t['description']}" + (f"\n  参数: {params_desc}" if params_desc else ""))
    return "\n".join(lines)


# ── ReAct 系统提示词 ──────────────────────────────────────────────────────────

REACT_SYSTEM_TEMPLATE = """你是一个智能 Agent。

{user_system_prompt}

## 核心原则：优先直接回答
**绝大多数问题你应该直接回答，不需要使用任何工具。**

只有在以下情况才使用工具：
- 需要获取**实时数据**（当前时间、实时天气、实时搜索结果）
- 需要进行**精确计算**（复杂数学表达式）
- 需要执行**真实 I/O**（HTTP 请求、数据库查询、发送邮件）
- 需要处理**用户提供的具体数据**（解析 JSON/CSV、提取手机号邮箱）

**以下情况绝对不要用工具，直接回答：**
- 翻译、写作、总结、解释、问答
- 单位换算、简单计算、常识性问题
- 任何你凭自身知识就能准确回答的问题

## 可用工具
{tools_desc}

## 输出格式
如果需要使用工具，严格按此格式（每次只执行一个动作）：

Thought: 分析是否真的需要工具，以及选择哪个工具
Action: 工具名称
Action Input: {{"参数名": "参数值"}}

获得工具结果后或无需工具时，直接给出最终答案：

Thought: 已有足够信息
Final Answer: 你的完整回答

## 富媒体输出规范
Final Answer 支持完整 Markdown 渲染，请充分利用：

- **可点击链接**：`[链接文字](https://URL)` — 搜索结果、参考资料必须用此格式，**不要把链接放进代码块**
- **内联图片**：`![图片描述](https://图片直链URL)` — 图片会直接在对话框内显示
- **B站视频**：`![视频标题](https://www.bilibili.com/video/BVxxxxxx)` — 会嵌入播放器
- **表格**：用 Markdown 表格展示结构化数据
- **代码块**：只用于展示真正的代码，**不要把普通文字或链接放进代码块**

⚠️ 重要限制（必须遵守）：
1. 链接和图片 URL 直接写在 Markdown 格式里，不要用代码块包裹
2. **图片只能使用国内可访问的直链**（如微博图床、阿里云OSS、腾讯云CDN、B站封面等），禁止使用 YouTube、Google、GitHub、placeholder.com 等境外图片源
3. **视频只嵌入 B站**（bilibili.com），禁止嵌入 YouTube 等境外视频
4. **严禁编造、猜测或虚构任何图片 URL**。如果你没有通过工具获取到真实可用的图片直链，就绝对不能输出 `![]()` 图片语法，必须改用文字描述。
5. 百度图片搜索页面链接（baidu.com/search）不是图片直链，禁止将其作为图片 URL 使用。
6. 只有通过 `web_search`、`http_request` 等工具实际获取到的图片 URL 才可以使用，不得凭记忆或推测填写图片地址。

## 严格规则
1. 如果不需要工具，**第一轮就直接输出 Final Answer**，不要先输出 Thought 再绕圈
2. Action Input 必须是合法 JSON
3. 工具调用失败时最多重试 1 次，之后直接用已有信息回答
4. 严禁循环调用同一工具超过 2 次
5. 最多使用工具 {max_iterations} 次，之后必须给出 Final Answer
"""


# ── ReAct 解析器 ──────────────────────────────────────────────────────────────

def parse_react_output(text: str) -> Dict[str, Any]:
    """解析 LLM 输出的 ReAct 格式"""
    # Final Answer
    fa_match = re.search(r"Final Answer[:：]\s*([\s\S]+?)$", text, re.IGNORECASE)
    if fa_match:
        return {"type": "final", "content": fa_match.group(1).strip()}

    # Action
    thought_match = re.search(r"Thought[:：]\s*(.*?)(?=Action[:：]|$)", text, re.DOTALL | re.IGNORECASE)
    action_match  = re.search(r"Action[:：]\s*(\w+)", text, re.IGNORECASE)
    input_match   = re.search(r"Action Input[:：]\s*(\{[\s\S]*?\})", text, re.IGNORECASE)

    if action_match:
        thought = thought_match.group(1).strip() if thought_match else ""
        action  = action_match.group(1).strip()
        try:
            action_input = json.loads(input_match.group(1)) if input_match else {}
        except Exception:
            action_input = {}
        return {"type": "action", "thought": thought, "action": action, "action_input": action_input}

    # 纯 Thought（无 Action）
    if thought_match:
        return {"type": "thought", "content": thought_match.group(1).strip()}

    return {"type": "unknown", "content": text}


# ── ReAct 引擎主体 ────────────────────────────────────────────────────────────

class AgentEngine:

    @staticmethod
    async def run_stream(
        agent_config: Dict,
        messages: List[Dict],
        db: AsyncSession,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式执行 ReAct 循环，每一步通过 yield 推送事件：
          {"type": "thought",      "content": "..."}
          {"type": "action",       "tool": "...", "input": {...}}
          {"type": "observation",  "content": "..."}
          {"type": "final",        "content": "..."}
          {"type": "error",        "content": "..."}
          {"type": "done"}
        """
        from app.models.model_provider import ModelConfig, ModelProvider
        from app.services.model_service import ModelService
        from app.schemas.model_provider import ChatRequest, ChatMessage

        model_config_id = agent_config.get("model_config_id")
        max_iterations  = int(agent_config.get("max_iterations", 10))
        temperature     = float(agent_config.get("temperature", 0.7))

        if not model_config_id:
            yield {"type": "error", "content": "Agent 未配置模型"}
            yield {"type": "done"}
            return

        # 加载模型
        res = await db.execute(select(ModelConfig).where(ModelConfig.id == model_config_id))
        model_config = res.scalar_one_or_none()
        if not model_config:
            yield {"type": "error", "content": "模型配置不存在"}
            yield {"type": "done"}
            return

        res2 = await db.execute(select(ModelProvider).where(ModelProvider.id == model_config.provider_id))
        provider = res2.scalar_one_or_none()
        if not provider:
            yield {"type": "error", "content": "模型供应商不存在"}
            yield {"type": "done"}
            return

        # 构建工具列表
        enabled_tool_names = [t["name"] for t in agent_config.get("tools", [])]
        enabled_tools = [t for t in BUILTIN_TOOLS if t["name"] in enabled_tool_names]
        # 加入自定义工具描述
        for ct in agent_config.get("custom_tools", []):
            enabled_tools.append(ct)

        tools_desc = tools_to_prompt(enabled_tools) if enabled_tools else "（未配置工具，直接回答）"

        # 系统提示
        system_prompt = REACT_SYSTEM_TEMPLATE.format(
            user_system_prompt=agent_config.get("system_prompt", ""),
            tools_desc=tools_desc,
            max_iterations=max_iterations,
        )

        # 构建对话历史（只取用户/助手消息，不含内部 ReAct 步骤）
        chat_history = []
        for m in messages[:-1]:  # 除最后一条（当前用户输入）
            if m["role"] in ("user", "assistant"):
                content = m["content"]
                if m["role"] == "assistant":
                    # 过滤掉历史回答中的图片/视频 markdown，防止 AI 在新回答中复用历史媒体链接
                    content = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'[图片/视频已省略: \1]', content)
                chat_history.append(ChatMessage(role=m["role"], content=content))

        user_input = messages[-1]["content"] if messages else ""

        # ReAct 工作内存（追加 Thought/Action/Observation）
        working_memory = list(chat_history)
        working_memory.append(ChatMessage(role="user", content=user_input))

        executor = ToolExecutor(db, agent_config)
        tool_call_counts: Dict[str, int] = {}  # 防止同一工具被重复调用

        llm_output = ""  # 保存最后一轮输出，供 else 分支使用

        for iteration in range(max_iterations):
            # ── 非最终轮：非流式调用，用于 ReAct 解析 ──────────────────────────
            req = ChatRequest(
                model_config_id=model_config_id,
                messages=working_memory,
                system_prompt=system_prompt,
                stream=False,
                temperature=temperature,
            )
            try:
                resp = await ModelService.chat(provider, model_config, req)
                llm_output = resp.get("content", "")
            except Exception as e:
                yield {"type": "error", "content": f"模型调用失败: {str(e)}"}
                break

            parsed = parse_react_output(llm_output)

            if parsed["type"] == "final":
                # ── 最终回答：改为流式逐 token 推送 ────────────────────────────
                final_text = parsed["content"]
                yield {"type": "final_start", "iteration": iteration + 1}
                # 逐 token 流式推送
                stream_req = ChatRequest(
                    model_config_id=model_config_id,
                    messages=working_memory + [
                        ChatMessage(role="assistant", content=llm_output.split("Final Answer")[0]),
                        ChatMessage(role="user", content="请直接输出你的最终回答内容，不要加任何前缀。"),
                    ],
                    system_prompt="你是一个助手，请直接输出用户要求的内容，不要加任何额外说明。",
                    stream=True,
                    temperature=temperature,
                )
                streamed = ""
                try:
                    async for chunk in ModelService.chat_stream(provider, model_config, stream_req):
                        if chunk["type"] == "content":
                            streamed += chunk["content"]
                            yield {"type": "final_chunk", "content": chunk["content"]}
                        elif chunk["type"] == "done":
                            break
                    # 如果流式内容为空（模型不支持），回退到非流式内容
                    if not streamed.strip():
                        streamed = final_text
                        yield {"type": "final_chunk", "content": final_text}
                except Exception:
                    streamed = final_text
                    yield {"type": "final_chunk", "content": final_text}

                yield {"type": "final", "content": streamed or final_text, "iteration": iteration + 1}
                break

            elif parsed["type"] == "action":
                if parsed.get("thought"):
                    yield {"type": "thought", "content": parsed["thought"], "iteration": iteration + 1}

                tool_name  = parsed["action"]
                tool_input = parsed["action_input"]

                # 防止同一工具被重复调用超过 2 次
                tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
                if tool_call_counts[tool_name] > 2:
                    final_text = f"工具 '{tool_name}' 已多次调用，以当前已知信息作答：\n\n" + llm_output
                    yield {"type": "final_start", "iteration": iteration + 1}
                    yield {"type": "final_chunk", "content": final_text}
                    yield {"type": "final", "content": final_text, "iteration": iteration + 1}
                    break

                yield {"type": "action", "tool": tool_name, "input": tool_input, "iteration": iteration + 1}

                observation = await executor.execute(tool_name, tool_input)
                yield {"type": "observation", "content": observation, "iteration": iteration + 1}

                working_memory.append(ChatMessage(role="assistant", content=llm_output))
                obs_msg = f"Observation: {observation}"
                working_memory.append(ChatMessage(role="user", content=obs_msg))

            elif parsed["type"] == "thought":
                yield {"type": "thought", "content": parsed["content"], "iteration": iteration + 1}
                working_memory.append(ChatMessage(role="assistant", content=llm_output))
                working_memory.append(ChatMessage(role="user", content="请直接给出 Final Answer，不要再调用工具。"))

            else:
                # 无法解析，当作 Final Answer 流式推送
                yield {"type": "final_start", "iteration": iteration + 1}
                yield {"type": "final_chunk", "content": llm_output}
                yield {"type": "final", "content": llm_output, "iteration": iteration + 1}
                break

        else:
            # 超过最大迭代次数
            final_text = "已达到最大迭代次数，以下是我目前的分析结果：\n\n" + llm_output
            yield {"type": "final_start", "iteration": max_iterations}
            yield {"type": "final_chunk", "content": final_text}
            yield {"type": "final", "content": final_text, "iteration": max_iterations}

        yield {"type": "done"}

"""Agent ReAct 执行引擎（v2.1 全面修复版）

关键修复：
1. 消除 Final Answer 双重模型调用 → 直接逐字符流式输出已验证内容
2. 行为级死循环检测 → 不止检测文本相似，还检测 action+input 重复
3. 追问机制（clarification）→ 信息不足时向用户要信息而不是硬猜
4. 工作内存压缩 → 超过阈值自动摘要旧步骤
5. 多轮意图追踪 → 基于会话主题而非仅最后一条消息
6. 强制回答输出清洗 → 去除 Thought/Action 格式残留
"""
import re
import time
import uuid
import asyncio
from typing import AsyncGenerator, Dict, Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger, trace_id_var, agent_id_var, PerformanceTimer

from app.services.agent.output_parser import (
    parse_react_output, validate_output_format, ParsedOutput
)
from app.services.agent.prompt_builder import PromptBuilder, RICH_MEDIA_RULES
from app.services.agent.context_manager import ContextManager, estimate_tokens
from app.services.agent.hallucination import HallucinationGuard, SelfVerifier
from app.services.agent.tool_executor import EnhancedToolExecutor
from app.services.agent.trace import (
    ExecutionTrace, PerformanceMonitor, IntentDriftDetector, get_global_monitor
)
from app.services.agent.checkpoint import CheckpointManager, ErrorRecovery
from app.services.tools import BUILTIN_TOOLS

logger = get_logger("engine")

# 工作内存中保留的最大 assistant+user 消息对数（超过则压缩旧的）
_MAX_WORKING_MEMORY_PAIRS = 6


def _clean_final_text(text: str) -> str:
    """清洗 Final Answer 文本，去除 ReAct 格式残留"""
    # 去掉 Thought: 行
    text = re.sub(r'^\s*Thought\s*[:：].*\n?', '', text, flags=re.MULTILINE | re.IGNORECASE)
    # 去掉 Action/Action Input 行
    text = re.sub(r'^\s*Action\s*(?:Input)?\s*[:：].*\n?', '', text, flags=re.MULTILINE | re.IGNORECASE)
    # 去掉 Final Answer: 前缀（如果整段都是）
    text = re.sub(r'^\s*Final\s*Answer\s*[:：]\s*', '', text, flags=re.IGNORECASE)
    return text.strip()


def _compress_working_memory(working_memory: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    压缩工作内存：保留第 1 条（用户原始输入）+ 最近 N 对
    旧步骤压缩为摘要行
    """
    if len(working_memory) <= 1 + _MAX_WORKING_MEMORY_PAIRS * 2:
        return working_memory

    first_msg = working_memory[0]  # 用户原始输入
    recent = working_memory[-(2 * _MAX_WORKING_MEMORY_PAIRS):]  # 最近 N 对
    old = working_memory[1:-(2 * _MAX_WORKING_MEMORY_PAIRS)]

    # 从旧步骤中提取摘要
    old_summary_parts = []
    for m in old:
        c = m.get("content", "")
        if m["role"] == "assistant":
            # 提取 Action 名
            act_m = re.search(r'Action\s*[:：]\s*(\S+)', c)
            if act_m:
                old_summary_parts.append(f"调用了工具 {act_m.group(1)}")
        elif m["role"] == "user":
            # 提取 Observation 摘要
            obs_m = re.search(r'Observation.*?[:：]\s*(.{0,80})', c, re.DOTALL)
            if obs_m:
                old_summary_parts.append(f"得到结果: {obs_m.group(1).strip()[:60]}")

    summary_text = "【已完成的步骤摘要】" + "；".join(old_summary_parts[-4:]) if old_summary_parts else ""

    compressed = [first_msg]
    if summary_text:
        compressed.append({"role": "user", "content": summary_text})
    compressed.extend(recent)
    return compressed


def _detect_action_loop(
    action_history: List[tuple],
    current_action: str,
    current_input: Dict,
) -> bool:
    """
    行为级死循环检测：同一工具+同样参数出现 >=2 次即判定
    """
    # 参数序列化用于比较（排序键确保一致）
    try:
        input_key = str(sorted(current_input.items())) if current_input else ""
    except Exception:
        input_key = str(current_input)

    action_key = (current_action, input_key)
    count = sum(1 for h in action_history if h == action_key)
    return count >= 2


def _is_truncated(text: str) -> bool:
    """
    检测模型输出是否被截断（未完成）。
    特征：末尾不是正常结束符，或者在 Markdown 格式中途断开。
    """
    if not text or len(text) < 20:
        return False
    text = text.rstrip()

    # 优先检查 Markdown 结构断裂（必须在 normal_endings 检查之前）
    last_line = text.split('\n')[-1].strip()
    if last_line.count('**') % 2 != 0:
        return True
    if last_line.count('*') % 2 != 0 and '**' not in last_line:
        return True
    # 未闭合的 Markdown 代码块
    if text.count('```') % 2 != 0:
        return True
    # Markdown 表格行中途断开（有 | 但不以 | 结尾）
    if last_line.startswith('|') and not last_line.endswith('|'):
        return True

    # 正常结束符
    normal_endings = ('。', '！', '？', '.', '!', '?', '```', '|', '）', ')', '>', '\n', '：', ':', '；', ';')
    if text.endswith(normal_endings):
        return False

    # 末尾是汉字/字母/逗号且文本较长 → 很可能被截断
    last_char = text[-1]
    if len(text) > 100 and (
        last_char.isalpha()
        or '\u4e00' <= last_char <= '\u9fff'
        or last_char in (',', '，', '、', '-', '—')
    ):
        return True
    return False


class AgentEngine:
    """
    增强版 ReAct Agent 执行引擎 (v2.1)

    事件类型：
      {"type": "thought",       "content": "...", "iteration": N}
      {"type": "action",        "tool": "...", "input": {...}, "iteration": N}
      {"type": "observation",   "content": "...", "iteration": N}
      {"type": "clarify",       "content": "...", "suggestions": [...]}
      {"type": "final_start",   "iteration": N}
      {"type": "final_chunk",   "content": "..."}
      {"type": "final",         "content": "...", "iteration": N}
      {"type": "error",         "content": "..."}
      {"type": "verify",        "content": "...", "iteration": N}
      {"type": "trace",         "summary": {...}}
      {"type": "done"}
    """

    @staticmethod
    async def run_stream(
        agent_config: Dict,
        messages: List[Dict],
        db: AsyncSession,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        from app.models.model_provider import ModelConfig, ModelProvider
        from app.services.model_service import ModelService
        from app.schemas.model_provider import ChatRequest, ChatMessage

        # ── 初始化 ────────────────────────────────────────────────────────
        trace_id = str(uuid.uuid4())[:12]
        trace_id_var.set(trace_id)
        agent_id_var.set(str(agent_config.get("id", "")))

        model_config_id = agent_config.get("model_config_id")
        max_iterations = int(agent_config.get("max_iterations", settings.AGENT_MAX_ITERATIONS))
        temperature = float(agent_config.get("temperature", settings.AGENT_TEMPERATURE))

        if settings.DETERMINISTIC_MODE:
            temperature = 0.0

        if not model_config_id:
            yield {"type": "error", "content": "Agent 未配置模型"}
            yield {"type": "done"}
            return

        # ── 加载模型 ──────────────────────────────────────────────────────
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

        # ── 构建工具列表 ──────────────────────────────────────────────────
        enabled_tool_names = [t["name"] for t in agent_config.get("tools", [])]
        enabled_tools = [t for t in BUILTIN_TOOLS if t["name"] in enabled_tool_names]
        for ct in agent_config.get("custom_tools", []):
            enabled_tools.append(ct)

        available_tool_names = [t["name"] for t in enabled_tools]

        # ── 初始化子系统 ──────────────────────────────────────────────────
        context_mgr = ContextManager()
        tool_executor = EnhancedToolExecutor(db, agent_config)
        error_recovery = ErrorRecovery()
        checkpoint_mgr = CheckpointManager(trace_id)
        monitor = get_global_monitor()

        # 执行轨迹
        user_input = messages[-1]["content"] if messages else ""

        # Q&A 编排层可要求先澄清，避免模型盲猜
        qa_plan = agent_config.get("_qa_plan") or {}
        if qa_plan.get("should_clarify") and qa_plan.get("clarify_question"):
            clarify_q = str(qa_plan.get("clarify_question"))
            yield {
                "type": "clarify",
                "content": clarify_q,
                "suggestions": ["补充目标", "补充约束", "补充输入范围"],
                "iteration": 0,
            }
            yield {"type": "done"}
            return

        trace = ExecutionTrace(
            trace_id=trace_id,
            agent_id=agent_config.get("id"),
            original_task=user_input,
        )
        trace.start_time = time.time()

        # ── 多轮意图追踪 ─────────────────────────────────────────────────
        # 用整个会话历史来确定会话主题，而非仅最后一条消息
        conversation_topic = _extract_conversation_topic(messages)
        drift_detector = IntentDriftDetector(conversation_topic)

        # ── 构建系统提示词 ────────────────────────────────────────────────
        memory_context = agent_config.get("_memory_context", "")
        system_prompt = PromptBuilder.build_system_prompt(
            user_system_prompt=agent_config.get("system_prompt", ""),
            tools=enabled_tools,
            max_iterations=max_iterations,
            memory_context=memory_context,
            extra_instructions=RICH_MEDIA_RULES,
            db_info=agent_config.get("_db_info"),
            qa_plan=agent_config.get("_qa_plan"),
        )

        # ── 构建对话历史（预清洗 assistant 消息中的 ReAct 步骤）─────────
        chat_history = []
        for m in messages[:-1]:
            if m["role"] in ("user", "assistant"):
                content = m["content"]
                if m["role"] == "assistant":
                    # 只保留 Final Answer 部分，去除 ReAct 中间步骤
                    content = _clean_final_text(content)
                    content = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'[媒体已省略: \1]', content)
                    if not content.strip():
                        continue
                chat_history.append({"role": m["role"], "content": content})

        # ReAct 工作内存
        working_memory: List[Dict[str, str]] = [{"role": "user", "content": user_input}]

        # 收集 observations 用于幻觉检查
        all_observations: List[Dict[str, str]] = []
        all_thoughts: List[str] = []
        action_history: List[tuple] = []  # 行为级循环检测

        # 历史摘要（如果需要）
        history_summary = ""
        if context_mgr.should_summarize(chat_history):
            history_summary = await _generate_summary(
                provider, model_config, chat_history, temperature
            )
            trace.add_step("context", "触发历史摘要压缩", token_savings=estimate_tokens(str(chat_history)))

        llm_output = ""
        request_start = time.time()

        # ── ReAct 主循环 ──────────────────────────────────────────────────
        for iteration in range(max_iterations):
            # ── 工作内存压缩 ───────────────────────────────────────────
            working_memory = _compress_working_memory(working_memory)

            # ── 构建受管理的上下文 ────────────────────────────────────────
            managed = context_mgr.build_context(
                system_prompt=system_prompt,
                chat_history=chat_history,
                working_memory=working_memory,
                original_task=user_input,
                memory_context=memory_context,
                history_summary=history_summary,
            )

            ctx_messages = [
                ChatMessage(role=m["role"], content=m["content"])
                for m in managed.messages
            ]

            # ── 模型推理 ─────────────────────────────────────────────────
            inference_start = time.perf_counter()
            req = ChatRequest(
                model_config_id=model_config_id,
                messages=ctx_messages,
                system_prompt=system_prompt,
                stream=False,
                temperature=temperature,
                max_tokens=settings.INFERENCE_MAX_OUTPUT_TOKENS,
            )

            try:
                resp = await ModelService.chat(provider, model_config, req)
                llm_output = resp.get("content", "")
                tokens_used = resp.get("usage", {})
            except Exception as e:
                yield {"type": "error", "content": f"模型调用失败: {str(e)}"}
                trace.add_step("error", f"模型调用失败: {e}")
                trace.success = False
                trace.error = str(e)
                break

            inference_ms = (time.perf_counter() - inference_start) * 1000
            output_tokens = tokens_used.get("completion_tokens", 0) if isinstance(tokens_used, dict) else 0
            monitor.record_inference(inference_ms, output_tokens)
            trace.total_tokens_used += tokens_used.get("total_tokens", 0) if isinstance(tokens_used, dict) else 0

            logger.trace_step(iteration + 1, "inference", f"耗时 {inference_ms:.0f}ms")

            # ── 死循环检测（文本级）──────────────────────────────────────
            if error_recovery.detect_loop(llm_output):
                force_msg = PromptBuilder.build_force_answer_message("检测到重复输出，请立即给出最终答案")
                working_memory.append({"role": "assistant", "content": llm_output})
                working_memory.append({"role": "user", "content": force_msg})
                trace.add_step("error", "检测到死循环（文本级），强制终止")
                continue

            # ── 解析输出 ──────────────────────────────────────────────────
            parsed = parse_react_output(llm_output, available_tool_names)

            # ── 检测追问意图 ──────────────────────────────────────────────
            clarify = _detect_clarification(llm_output, parsed)
            if clarify:
                yield {
                    "type": "clarify",
                    "content": clarify["question"],
                    "suggestions": clarify.get("suggestions", []),
                    "iteration": iteration + 1,
                }
                # 追问时终止循环，等待用户下一轮输入
                trace.add_step("clarify", clarify["question"][:200])
                trace.final_answer = clarify["question"]
                # 同时作为 final 事件发出，让前端正确保存
                yield {"type": "final_start", "iteration": iteration + 1}
                yield {"type": "final_chunk", "content": clarify["question"]}
                yield {"type": "final", "content": clarify["question"], "iteration": iteration + 1}
                break

            # ── 格式校验 ─────────────────────────────────────────────────
            errors = validate_output_format(parsed)

            if errors and parsed.type == "unknown":
                recovery = error_recovery.handle_parse_error(llm_output, iteration)

                if recovery["action"] == "force_answer":
                    # 连续格式错误太多 → 清洗后作为 Final Answer
                    final_text = _clean_final_text(llm_output)
                    if not final_text.strip():
                        final_text = "抱歉，处理过程中遇到了问题，请重新描述您的需求。"
                    yield {"type": "final_start", "iteration": iteration + 1}
                    yield {"type": "final_chunk", "content": final_text}
                    yield {"type": "final", "content": final_text, "iteration": iteration + 1}
                    trace.add_step("final", final_text[:200], forced=True)
                    trace.final_answer = final_text
                    break

                working_memory.append({"role": "assistant", "content": llm_output})
                working_memory.append({"role": "user", "content": recovery["message"]})
                trace.add_step("error", f"输出格式错误: {'; '.join(errors)}")
                continue

            error_recovery.reset_error_count()

            # ── 处理 Final Answer ────────────────────────────────────────
            if parsed.type == "final":
                # 先发出 Thought（如果有），保证思维链完整可见
                if parsed.thought:
                    yield {"type": "thought", "content": parsed.thought, "iteration": iteration + 1}
                    all_thoughts.append(parsed.thought)
                    trace.add_step("thought", parsed.thought[:200])

                final_text = parsed.content

                # ── 截断检测：如果回答被截断且还有迭代余量，请求继续 ──
                if _is_truncated(final_text) and iteration < max_iterations - 1:
                    continuation_prompt = (
                        f"你的回答被截断了，请从以下位置继续输出（不要重复已输出的内容）：\n"
                        f"...{final_text[-80:]}\n\n"
                        f"请直接继续输出剩余内容，以 Final Answer: 开头，接上被截断的部分。"
                    )
                    working_memory.append({"role": "assistant", "content": llm_output})
                    working_memory.append({"role": "user", "content": continuation_prompt})
                    trace.add_step("truncation", "检测到输出截断，请求继续")
                    # 保存已有内容用于拼接
                    if not trace.partial_final:
                        trace.partial_final = final_text
                    else:
                        trace.partial_final += final_text
                    continue

                # 如果之前有截断续写，拼接完整内容
                if trace.partial_final:
                    final_text = trace.partial_final + final_text
                    trace.partial_final = ""

                trace.add_step("final", final_text[:200])

                # ── 幻觉抑制校验 ─────────────────────────────────────────
                observations = tool_executor.get_observations_for_verify()

                if observations:
                    verify_result = HallucinationGuard.verify_against_observations(
                        final_text, observations
                    )
                    if not verify_result.passed:
                        trace.add_step("verify", f"幻觉检查未通过: {verify_result.issues}")
                        monitor.increment("hallucination_detections")
                        final_text = HallucinationGuard.add_uncertainty_markers(
                            final_text, verify_result.confidence
                        )

                    final_text = HallucinationGuard.enforce_source_citation(
                        final_text, observations
                    )

                # 自洽性检查
                consistency = HallucinationGuard.check_self_consistency(all_thoughts, final_text)
                if not consistency.passed:
                    trace.add_step("verify", f"自洽性问题: {consistency.issues}")

                # ── 自我校验（可选）─────────────────────────────────────
                if SelfVerifier.should_verify(final_text, observations, iteration + 1):
                    verify_passed, corrected = await _self_verify(
                        provider, model_config, user_input,
                        final_text, observations, temperature
                    )
                    if not verify_passed and corrected:
                        # 防止自我校验产生截断的修正（比原文短很多说明被截断了）
                        if len(corrected) >= len(final_text) * 0.4 and len(corrected) > 50:
                            yield {"type": "verify", "content": "自我校验发现问题，已修正", "iteration": iteration + 1}
                            final_text = corrected
                            trace.add_step("verify", "自我校验修正")
                        else:
                            # 修正太短，保留原文但加提示
                            trace.add_step("verify", f"自我校验修正被跳过（修正文本过短: {len(corrected)}字 vs 原文{len(final_text)}字）")

                # ── 直接逐字符流式输出（不再二次调用模型）────────────────
                yield {"type": "final_start", "iteration": iteration + 1}

                # 分块发送（模拟流式效果，每块 20-80 字符）
                chunk_size = 40
                for i in range(0, len(final_text), chunk_size):
                    chunk = final_text[i:i + chunk_size]
                    yield {"type": "final_chunk", "content": chunk}
                    await asyncio.sleep(0.01)  # 微延迟保证前端能逐步渲染

                trace.final_answer = final_text
                yield {"type": "final", "content": final_text, "iteration": iteration + 1}
                break

            # ── 处理 Action ──────────────────────────────────────────────
            elif parsed.type == "action":
                if parsed.thought:
                    yield {"type": "thought", "content": parsed.thought, "iteration": iteration + 1}
                    all_thoughts.append(parsed.thought)
                    trace.add_step("thought", parsed.thought[:200])

                    # 意图漂移检测（使用会话主题而非仅当前问题）
                    if drift_detector.check_drift(parsed.thought):
                        drift_msg = (
                            f"⚠️ 请注意，你当前的思考可能偏离了用户的任务。\n"
                            f"用户当前问题是: {user_input[:100]}\n"
                            f"会话主题是: {conversation_topic[:100]}\n"
                            f"请重新聚焦于用户的问题。"
                        )
                        working_memory.append({"role": "assistant", "content": llm_output})
                        working_memory.append({"role": "user", "content": drift_msg})
                        trace.add_step("drift", "检测到意图漂移，已纠正")
                        continue

                tool_name = parsed.action
                tool_input = parsed.action_input

                # ── 行为级死循环检测 ─────────────────────────────────────
                if _detect_action_loop(action_history, tool_name, tool_input):
                    force_msg = (
                        f"你已经用相同参数调用 {tool_name} 多次了，结果不会改变。"
                        f"请基于已有的工具返回结果给出 Final Answer。"
                    )
                    working_memory.append({"role": "assistant", "content": llm_output})
                    working_memory.append({"role": "user", "content": force_msg})
                    trace.add_step("error", f"行为级死循环: {tool_name} 重复调用")
                    continue

                # 记录行为历史
                try:
                    input_key = str(sorted(tool_input.items())) if tool_input else ""
                except Exception:
                    input_key = str(tool_input)
                action_history.append((tool_name, input_key))

                # 检查工具调用限制
                if tool_executor.has_exceeded_tool_limit(tool_name):
                    force_msg = f"工具 '{tool_name}' 已达到调用上限，请基于已有信息给出 Final Answer。"
                    working_memory.append({"role": "assistant", "content": llm_output})
                    working_memory.append({"role": "user", "content": force_msg})
                    trace.add_step("limit", f"工具 {tool_name} 超过调用限制")
                    continue

                yield {"type": "action", "tool": tool_name, "input": tool_input, "iteration": iteration + 1}

                # ── 执行工具（带超时和重试）──────────────────────────────
                tool_start = time.perf_counter()
                observation = await tool_executor.execute(tool_name, tool_input)
                tool_ms = (time.perf_counter() - tool_start) * 1000
                monitor.record_tool_call(tool_ms, "失败" not in observation and "错误" not in observation)

                yield {"type": "observation", "content": observation, "iteration": iteration + 1}

                all_observations.append({"tool_name": tool_name, "content": observation})
                trace.add_step("action", f"调用 {tool_name}", duration_ms=tool_ms)
                trace.add_step("observation", observation[:200])
                trace.total_tool_calls += 1

                # 防伪造检查
                fabrication_check = HallucinationGuard.check_fabricated_tool_results(
                    llm_output, [observation]
                )
                if not fabrication_check.passed:
                    trace.add_step("verify", f"检测到工具结果伪造: {fabrication_check.issues}")

                # 构建 observation 消息
                obs_msg = PromptBuilder.build_observation_message(tool_name, observation)
                working_memory.append({"role": "assistant", "content": llm_output})
                working_memory.append({"role": "user", "content": obs_msg})

                # 保存检查点
                checkpoint_mgr.save(
                    iteration=iteration,
                    working_memory=[{"role": m["role"], "content": m["content"][:500]} for m in working_memory],
                    tool_history=[r.to_dict() for r in tool_executor.call_history],
                    observations=all_observations,
                )

            # ── 处理纯 Thought ───────────────────────────────────────────
            elif parsed.type == "thought":
                yield {"type": "thought", "content": parsed.content, "iteration": iteration + 1}
                all_thoughts.append(parsed.content)
                trace.add_step("thought", parsed.content[:200])

                working_memory.append({"role": "assistant", "content": llm_output})
                force_msg = PromptBuilder.build_force_answer_message("你已输出 Thought 但未选择工具也未给出 Final Answer")
                working_memory.append({"role": "user", "content": force_msg})

            else:
                # 无法解析 → 清洗后作为 Final Answer
                clean = _clean_final_text(llm_output)
                if not clean.strip():
                    clean = "抱歉，处理过程中遇到了问题，请重新描述您的需求。"
                yield {"type": "final_start", "iteration": iteration + 1}
                yield {"type": "final_chunk", "content": clean}
                yield {"type": "final", "content": clean, "iteration": iteration + 1}
                trace.final_answer = clean
                trace.add_step("final", clean[:200], fallback=True)
                break

        else:
            # ── 超过最大迭代次数 ─────────────────────────────────────────
            summary = _build_iteration_limit_answer(llm_output, all_observations)
            yield {"type": "final_start", "iteration": max_iterations}
            yield {"type": "final_chunk", "content": summary}
            yield {"type": "final", "content": summary, "iteration": max_iterations}
            trace.final_answer = summary
            trace.add_step("final", "超过最大迭代次数", forced=True)

        # ── 清理与记录 ────────────────────────────────────────────────────
        # 释放外部数据库连接
        await tool_executor.close_external_engine()

        trace.end_time = time.time()
        trace.total_iterations = min(iteration + 1, max_iterations) if 'iteration' in dir() else 0
        total_ms = trace.duration_ms
        monitor.record_request(total_ms, trace.success)

        checkpoint_mgr.clear()

        if settings.LOG_AGENT_TRACE:
            logger.info(trace.to_summary())

        yield {"type": "trace", "summary": trace.to_dict()}
        yield {"type": "done"}


# ── 追问检测 ─────────────────────────────────────────────────────────────────

def _detect_clarification(llm_output: str, parsed: ParsedOutput) -> Optional[Dict]:
    """
    检测模型是否在尝试追问用户。

    模式：
    1. Final Answer 中包含明确的提问句式
    2. 模型直接输出了问句而没有 ReAct 格式
    """
    text = parsed.content if parsed.type == "final" else llm_output
    if not text:
        return None

    # 追问特征检测（中文+英文）
    question_patterns = [
        r'(?:请(?:问|告诉|提供|说明)|你能(?:否|不能)?(?:告诉|提供|给)|需要你(?:提供|告诉|说明)).*[？?]',
        r'(?:请问|请提供|请告诉|请输入|请给出).*(?:信息|数据|内容|条件|参数|名称)',
        r'(?:需要|还需要|缺少).*(?:哪些|什么|以下).*(?:信息|参数|条件)',
        r'(?:Could you|Can you|Please provide|Please tell|What is|Which).*\?',
    ]

    is_question = False
    for pattern in question_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            is_question = True
            break

    # 也检测末尾是否是问号结尾的短文本
    if not is_question:
        lines = text.strip().split('\n')
        last_line = lines[-1].strip()
        if (last_line.endswith('？') or last_line.endswith('?')) and len(text) < 500:
            is_question = True

    if not is_question:
        return None

    # 提取建议选项（如果有列表形式的选项）
    suggestions = []
    option_patterns = [
        r'[1-9][.)、]\s*(.{2,30})',
        r'[-·•]\s*(.{2,30})',
    ]
    for pat in option_patterns:
        matches = re.findall(pat, text)
        if 2 <= len(matches) <= 6:
            suggestions = [m.strip() for m in matches]
            break

    return {"question": text, "suggestions": suggestions}


def _extract_conversation_topic(messages: List[Dict]) -> str:
    """
    从整个会话历史中提取会话主题（用于意图漂移检测）
    优先使用第一条用户消息 + 最后一条用户消息的组合
    """
    user_messages = [m["content"] for m in messages if m.get("role") == "user"]
    if not user_messages:
        return ""
    if len(user_messages) == 1:
        return user_messages[0]
    # 第一条（确定会话主题）+ 最后一条（当前意图）
    return f"{user_messages[0][:100]}。当前问题：{user_messages[-1][:100]}"


def _build_iteration_limit_answer(
    last_output: str,
    observations: List[Dict[str, str]],
) -> str:
    """超过迭代上限时构建综合答案"""
    parts = ["已达到最大推理步数，以下是基于已有信息的分析结果：\n"]

    # 汇总工具结果
    if observations:
        parts.append("**已获取的信息：**")
        for obs in observations[-3:]:
            tool = obs.get("tool_name", "")
            content = obs.get("content", "")[:300]
            parts.append(f"- {tool}: {content}")
        parts.append("")

    # 添加清洗后的最后输出
    clean = _clean_final_text(last_output)
    if clean:
        parts.append(clean)
    else:
        parts.append("抱歉，未能在限定步数内完成分析。请尝试简化您的问题或提供更多具体信息。")

    return "\n".join(parts)


# ── 辅助异步函数 ─────────────────────────────────────────────────────────────

async def _generate_summary(
    provider, model_config, chat_history: List[Dict], temperature: float
) -> str:
    """生成历史摘要"""
    from app.services.model_service import ModelService
    from app.schemas.model_provider import ChatRequest, ChatMessage

    history_text = "\n".join(
        f"[{m['role']}]: {m['content'][:200]}" for m in chat_history[-10:]
    )

    summary_prompt = PromptBuilder.build_history_summary_prompt(
        history=history_text,
        max_tokens=200,
    )

    try:
        req = ChatRequest(
            model_config_id=model_config.id,
            messages=[ChatMessage(role="user", content=summary_prompt)],
            system_prompt="你是一个摘要助手，请简洁地压缩对话历史。",
            stream=False,
            temperature=temperature,
        )
        resp = await ModelService.chat(provider, model_config, req)
        return resp.get("content", "")[:500]
    except Exception as e:
        logger.error(f"生成历史摘要失败: {e}")
        return ""


async def _self_verify(
    provider, model_config, original_task: str,
    final_answer: str, observations: List[Dict],
    temperature: float,
) -> tuple:
    """执行自我校验"""
    from app.services.model_service import ModelService
    from app.schemas.model_provider import ChatRequest, ChatMessage

    verify_prompt = SelfVerifier.build_verify_prompt(
        original_task, final_answer, observations
    )

    try:
        req = ChatRequest(
            model_config_id=model_config.id,
            messages=[ChatMessage(role="user", content=verify_prompt)],
            system_prompt="你是一个质量检查员，请检查回答的准确性。",
            stream=False,
            temperature=0.1,
            max_tokens=2048,  # 30B 优化：限制 verify 输出长度，避免浪费 token
        )
        resp = await ModelService.chat(provider, model_config, req)
        verify_output = resp.get("content", "")
        return SelfVerifier.parse_verify_result(verify_output)
    except Exception as e:
        logger.error(f"自我校验失败: {e}")
        return True, ""

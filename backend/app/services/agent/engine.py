"""
Agent ReAct 执行引擎（增强版）

整合所有子模块，提供完整的 Agent 执行能力：
1. 增强 ReAct 循环控制（严格终止条件）
2. 子任务拆分与回溯
3. 意图漂移防止
4. 失败重试 + 错误恢复
5. 自我校验抑制幻觉
6. 上下文不干扰机制
7. 滑动窗口 + 历史蒸馏
8. 工具调用结果校验
9. 执行轨迹追踪
10. 断点续跑
"""
import re
import time
import uuid
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


class AgentEngine:
    """
    增强版 ReAct Agent 执行引擎

    执行流程：
    1. 初始化（加载模型、构建工具列表、设置上下文管理器）
    2. 构建系统提示词（含 30B 优化模板、Few-shot、记忆上下文）
    3. ReAct 循环：
       a. 构建受管理的上下文（滑动窗口 + 压缩）
       b. 调用模型推理
       c. 解析输出（多级容错）
       d. 格式校验 + 错误恢复
       e. 死循环检测
       f. 工具执行（超时 + 重试 + 权限）
       g. 工具结果校验（防幻觉）
       h. 意图漂移检测
       i. 保存检查点
    4. 最终回答：
       a. 幻觉抑制校验
       b. 来源引用强制
       c. 自我校验（可选）
       d. 流式输出
    5. 执行轨迹记录
    """

    @staticmethod
    async def run_stream(
        agent_config: Dict,
        messages: List[Dict],
        db: AsyncSession,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式执行 ReAct 循环

        事件类型：
          {"type": "thought",       "content": "...", "iteration": N}
          {"type": "action",        "tool": "...", "input": {...}, "iteration": N}
          {"type": "observation",   "content": "...", "iteration": N}
          {"type": "final_start",   "iteration": N}
          {"type": "final_chunk",   "content": "..."}
          {"type": "final",         "content": "...", "iteration": N}
          {"type": "error",         "content": "..."}
          {"type": "verify",        "content": "...", "iteration": N}
          {"type": "trace",         "summary": {...}}
          {"type": "done"}
        """
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

        # 确定性模式
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
        trace = ExecutionTrace(
            trace_id=trace_id,
            agent_id=agent_config.get("id"),
            original_task=messages[-1]["content"] if messages else "",
        )
        trace.start_time = time.time()

        # 意图漂移检测
        original_task = messages[-1]["content"] if messages else ""
        drift_detector = IntentDriftDetector(original_task)

        # ── 构建系统提示词 ────────────────────────────────────────────────
        memory_context = agent_config.get("_memory_context", "")
        system_prompt = PromptBuilder.build_system_prompt(
            user_system_prompt=agent_config.get("system_prompt", ""),
            tools=enabled_tools,
            max_iterations=max_iterations,
            memory_context=memory_context,
            extra_instructions=RICH_MEDIA_RULES,
        )

        # ── 构建对话历史 ──────────────────────────────────────────────────
        chat_history = []
        for m in messages[:-1]:
            if m["role"] in ("user", "assistant"):
                content = m["content"]
                if m["role"] == "assistant":
                    content = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'[媒体已省略: \1]', content)
                chat_history.append({"role": m["role"], "content": content})

        user_input = messages[-1]["content"] if messages else ""

        # ReAct 工作内存
        working_memory: List[Dict[str, str]] = [{"role": "user", "content": user_input}]

        # 收集 observations 用于幻觉检查
        all_observations: List[Dict[str, str]] = []
        all_thoughts: List[str] = []

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
            # ── 构建受管理的上下文 ────────────────────────────────────────
            managed = context_mgr.build_context(
                system_prompt=system_prompt,
                chat_history=chat_history,
                working_memory=working_memory,
                original_task=original_task,
                memory_context=memory_context,
                history_summary=history_summary,
            )

            # 转为 ChatMessage 列表
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

            # ── 死循环检测 ────────────────────────────────────────────────
            if error_recovery.detect_loop(llm_output):
                force_msg = PromptBuilder.build_force_answer_message("检测到重复输出，强制终止")
                working_memory.append({"role": "assistant", "content": llm_output})
                working_memory.append({"role": "user", "content": force_msg})
                trace.add_step("error", "检测到死循环，强制终止")
                continue

            # ── 解析输出 ──────────────────────────────────────────────────
            parsed = parse_react_output(llm_output, available_tool_names)

            # ── 格式校验 ─────────────────────────────────────────────────
            errors = validate_output_format(parsed)

            if errors and parsed.type == "unknown":
                # 格式错误 → 错误恢复
                recovery = error_recovery.handle_parse_error(llm_output, iteration)

                if recovery["action"] == "force_answer":
                    # 连续格式错误太多，将原始输出作为 Final Answer
                    final_text = llm_output
                    yield {"type": "final_start", "iteration": iteration + 1}
                    yield {"type": "final_chunk", "content": final_text}
                    yield {"type": "final", "content": final_text, "iteration": iteration + 1}
                    trace.add_step("final", final_text, forced=True)
                    trace.final_answer = final_text
                    break

                # 给模型发送格式纠正提示
                working_memory.append({"role": "assistant", "content": llm_output})
                working_memory.append({"role": "user", "content": recovery["message"]})
                trace.add_step("error", f"输出格式错误: {'; '.join(errors)}")
                continue

            error_recovery.reset_error_count()

            # ── 处理 Final Answer ────────────────────────────────────────
            if parsed.type == "final":
                final_text = parsed.content
                trace.add_step("final", final_text[:200])

                # ── 幻觉抑制校验 ─────────────────────────────────────────
                observations = tool_executor.get_observations_for_verify()

                if observations:
                    # 工具结果一致性检查
                    verify_result = HallucinationGuard.verify_against_observations(
                        final_text, observations
                    )
                    if not verify_result.passed:
                        trace.add_step("verify", f"幻觉检查未通过: {verify_result.issues}")
                        monitor.increment("hallucination_detections")
                        # 添加不确定性标记
                        final_text = HallucinationGuard.add_uncertainty_markers(
                            final_text, verify_result.confidence
                        )

                    # 来源引用强制
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
                        provider, model_config, original_task,
                        final_text, observations, temperature
                    )
                    if not verify_passed and corrected:
                        yield {"type": "verify", "content": "自我校验发现问题，已修正", "iteration": iteration + 1}
                        final_text = corrected
                        trace.add_step("verify", "自我校验修正")

                # ── 流式输出最终回答 ─────────────────────────────────────
                yield {"type": "final_start", "iteration": iteration + 1}

                # 尝试流式重新生成
                stream_req = ChatRequest(
                    model_config_id=model_config_id,
                    messages=ctx_messages + [
                        ChatMessage(role="assistant", content=llm_output.split("Final Answer")[0] if "Final Answer" in llm_output else ""),
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
                    if not streamed.strip():
                        streamed = final_text
                        yield {"type": "final_chunk", "content": final_text}
                except Exception:
                    streamed = final_text
                    yield {"type": "final_chunk", "content": final_text}

                trace.final_answer = streamed or final_text
                yield {"type": "final", "content": streamed or final_text, "iteration": iteration + 1}
                break

            # ── 处理 Action ──────────────────────────────────────────────
            elif parsed.type == "action":
                if parsed.thought:
                    yield {"type": "thought", "content": parsed.thought, "iteration": iteration + 1}
                    all_thoughts.append(parsed.thought)
                    trace.add_step("thought", parsed.thought[:200])

                    # 意图漂移检测
                    if drift_detector.check_drift(parsed.thought):
                        drift_msg = (
                            f"⚠️ 请注意，你当前的思考可能偏离了原始任务。\n"
                            f"原始任务是: {original_task[:100]}\n"
                            f"请重新聚焦于原始任务。"
                        )
                        working_memory.append({"role": "assistant", "content": llm_output})
                        working_memory.append({"role": "user", "content": drift_msg})
                        trace.add_step("drift", "检测到意图漂移，已纠正")
                        continue

                tool_name = parsed.action
                tool_input = parsed.action_input

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

                # 记录观察结果
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

                # 构建 observation 消息（注意力引导）
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
                force_msg = PromptBuilder.build_force_answer_message("你已输出 Thought 但未选择工具")
                working_memory.append({"role": "user", "content": force_msg})

            else:
                # 无法解析 → 作为 Final Answer
                yield {"type": "final_start", "iteration": iteration + 1}
                yield {"type": "final_chunk", "content": llm_output}
                yield {"type": "final", "content": llm_output, "iteration": iteration + 1}
                trace.final_answer = llm_output
                trace.add_step("final", llm_output[:200], fallback=True)
                break

        else:
            # ── 超过最大迭代次数 ─────────────────────────────────────────
            final_text = "已达到最大推理步数，以下是基于已有信息的分析结果：\n\n" + llm_output
            yield {"type": "final_start", "iteration": max_iterations}
            yield {"type": "final_chunk", "content": final_text}
            yield {"type": "final", "content": final_text, "iteration": max_iterations}
            trace.final_answer = final_text
            trace.add_step("final", "超过最大迭代次数", forced=True)

        # ── 清理与记录 ────────────────────────────────────────────────────
        trace.end_time = time.time()
        trace.total_iterations = min(iteration + 1, max_iterations) if 'iteration' in dir() else 0
        total_ms = trace.duration_ms
        monitor.record_request(total_ms, trace.success)

        # 清理检查点
        checkpoint_mgr.clear()

        # 输出执行轨迹
        if settings.LOG_AGENT_TRACE:
            logger.info(trace.to_summary())

        yield {"type": "trace", "summary": trace.to_dict()}
        yield {"type": "done"}


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
            temperature=0.1,  # 校验用极低温度
        )
        resp = await ModelService.chat(provider, model_config, req)
        verify_output = resp.get("content", "")
        return SelfVerifier.parse_verify_result(verify_output)
    except Exception as e:
        logger.error(f"自我校验失败: {e}")
        return True, ""  # 校验失败时默认通过

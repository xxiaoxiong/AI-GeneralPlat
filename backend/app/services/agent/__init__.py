"""
Agent 智能体引擎包

模块架构：
  engine.py          - ReAct 核心执行引擎（增强版）
  output_parser.py   - 输出解析 + JSON 修复
  prompt_builder.py  - 30B 优化提示词工程
  context_manager.py - 上下文窗口管理（滑动窗口 + 摘要压缩）
  memory_manager.py  - 记忆系统（短期/长期/结构化）
  hallucination.py   - 幻觉抑制（事实校验/来源引用/自我校验）
  planner.py         - 任务规划与子任务拆分
  tool_executor.py   - 增强工具执行器（超时/重试/权限/依赖）
  trace.py           - 执行轨迹追踪与性能监控
  checkpoint.py      - 断点续跑与错误恢复
"""
from app.services.agent.engine import AgentEngine

__all__ = ["AgentEngine"]

# 00-baseline - 改造前现状、风险与测试基线

> 阶段一交付文档之一（任务书 §9.1）。记录 DeepAgents 内核重构**动手之前**的调用链、
> 关键风险、测试基线与代码版本，作为改造效果的对照基准。勘察日期：2026-11-09。

## 1. 代码版本

| 项 | 值 |
| --- | --- |
| 在线基线 | `main` @ `4d0f6b133fb8c0b7a8f2df8e803189c6ed36023a`（任务书给定，2026-03-15） |
| 本地起点 | 同一 commit `4d0f6b1`（本地工作区无领先提交，改造全部为未提交工作区改动） |
| 运行环境 | Windows / Python 3.11.9 / Node（前端）|
| 依赖现状（改造前） | FastAPI、OpenAI SDK、SQLAlchemy、aiomysql/pymysql、Redis；无 DeepAgents/LangGraph |
| 依赖现状（改造后） | 见 `03-operations.md` 依赖版本表（deepagents 0.7.34 等，已锁定进 `backend/requirements.txt`） |

## 2. 改造前调用链（现状证据）

```text
POST /api/v1/agents/{id}/chat（SSE）          backend/app/api/v1/agents.py
    └─ AgentEngine.run_stream(config, messages, db)   services/agent/engine.py
         ├─ for iteration in range(max_iterations):   ← 手写 ReAct 主循环
         │    prompt_builder 组装提示
         │    model_service 一次性拿全量文本（人为分块发 SSE，非真流式）
         │    output_parser 用正则解析 Thought/Action/Final Answer 文本协议
         │    tool_executor 执行工具（同步/异步/DB/AGENT handler）
         ├─ context_manager.py：滑窗 + 摘要（自研）
         ├─ checkpoint.py：每 run 本地 JSON 检查点（自研）
         └─ memory_manager.py：SQLite memory_v2.db（fact/rule/skill 记忆）

POST /api/v1/agents/{id}/invoke（非流式）    → 同 AgentEngine
workflow_service._exec_agent_invoke          → 同 AgentEngine（画布 Agent 节点）
services/agent_service.py                    → 另一套早期手写 ReAct（重复执行路径）
```

## 3. 改造前已识别关键风险（任务书 §2 + 复核结论）

| # | 风险 | 处置（本阶段） |
| --- | --- | --- |
| 1 | `agents.py` 部分读取/删除 Agent、Session 查询未绑定当前用户所有权 → 越权 | `_get_or_404`/会话查询/删除/Run 级资源全部加所有权校验（见 01 文档 §11） |
| 2 | `verify=False` 硬编码 8 处（tools/__init__、web_tools×3、network_tools×2、model_service、inference/model_manager、agent/tool_executor）→ TLS 校验关闭 | 全部修复为可配置 `AGENT_TLS_VERIFY`/`AGENT_CA_BUNDLE`；knowledge_service 1 处非 Agent 路径，标注 TODO 阶段二 |
| 3 | 旧工具自定义 HTTP 调用可探内网/云元数据 → SSRF | `check_url` 策略守卫 + allowlist/blocklist（01 文档 §11） |
| 4 | 最终输出一次性取得后人为分块 → 伪流式 | 新内核 `astream` 真 token 增量（AT-03 实测） |
| 5 | 工具无强类型 schema、错误靠字符串匹配"失败" | Tool Adapter 生成 pydantic 参数模型；工具失败=显式 TOOL_FAILED 状态 |
| 6 | 手写 ReAct 文本协议脆弱（正则解析） | 原生 tool calling，无文本协议解析器依赖 |
| 7 | 自研 JSON checkpoint / 滑窗摘要与新机制冲突 | LangGraph Checkpointer + 内置 SummarizationMiddleware 接管；旧模块退出执行路径 |
| 8 | 无运行预算/沙箱/审批 | ExecutionBudget + ToolPolicy 沙箱分级 + HITL interrupt_on |

## 4. 改造前测试基线

| 项 | 基线 | 证据 |
| --- | --- | --- |
| 后端既有单测 | **65 项全部通过**（改造前运行 `cd backend && python -m pytest tests/ -q`，唯 1 项需要真实模型/环境的用例以 skip 呈现） | 改造后全量 **119 passed + 3 skipped** 中，既有 65 项（tests/test_agent_optimizations.py）全部通过无回归（57 项为本次新增，其中 3 项真实模型 gated skip） |
| 前端构建 | `npx vite build` 通过；`vue-tsc` 有 4 个**无关旧文件** DefaultRow 类型告警（依赖漂移所致，非改造引入，如实记录） | 本机 npm 镜像（npmmirror）故障，依赖经官方 registry 安装，未改 package-lock.json |
| 数据库 | MySQL `localhost:3307`（DATABASE_URL 默认），本机不可达 → 见下 | 探测输出存 04-validation.md |
| 模型服务 | 本机无可用 OpenAI 兼容服务（localhost:3000/11434/8000/1234 探测均无响应） | 真实模型 E2E 改为环境变量 gate（tests/test_agent_runtime_e2e.py） |

**基线测试命令**（改造前后同一命令，结果对照）：

```powershell
cd backend
python -m pytest tests/ -q
```

- 改造前：`65 passed, 1 skipped`
- 改造后（最终）：`119 passed, 3 skipped`（新增 57 项 Runtime/集成测试 = 54 项确定性通过 + 3 项真实模型 gated；既有 65 项无回归）

## 5. 基线阶段确定的版本策略

DeepAgents/LangChain/LangGraph API 变动快（任务书要求"核对当前已安装版本与官方文档，
锁定相互兼容的版本并记录"）。实际做法：

1. 在 Python 3.11.9 中安装 deepagents 并解析其依赖，读取已安装包源码核实
   `create_deep_agent` / Middleware / Backend / HITL / Checkpointer 等 API 签名；
2. 锁定组合写入 `backend/requirements.txt`（版本见 03-operations.md）；
3. 全部关键 API 用法以**实测运行结果**为准（如模型节点名 `langgraph_node=="model"`、
   HITL `ActionRequest.name` 字段、InterruptOnConfig 为 dict 形态等），不照搬过时示例。

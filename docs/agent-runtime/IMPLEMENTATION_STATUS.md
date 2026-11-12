# IMPLEMENTATION_STATUS - 阶段一 DeepAgents 内核重构

> 更新：2026-11-09。状态口径：✅=已实现且有验证证据；⚠️=已实现但验证受限/有缺口如实注明；⛔=环境阻塞（有证据，非代码缺陷）；❌=未做。
> AT 编号与任务书 §8 验收场景一一对应；逐项证据见 [04-validation.md](04-validation.md)。

## 一、验收项对照（任务书 §8 AT-01 ~ AT-18）

| AT | 要求 | 状态 | 证据（详见 04-validation.md） |
| --- | --- | --- | --- |
| 01 | 旧 API 聊天/会话不被破坏 | ✅ | TestChatFlow（HTTP：/chat SSE、/invoke、会话历史、删除 404）+ TestWorkflowAgentNode；MySQL 复验 ⛔（DDL 离线生成已验） |
| 02 | 原生工具调用（真实模型 tool calling，无文本 ReAct 解析） | ⛔/✅ | 确定性 Fake Model 多步 tool calling E2E + demo 真工具执行 ✅；真实模型 gated（AGENT_E2E_*）⛔；无文本 ReAct 解析器 |
| 03 | 流式输出（真实 token delta、断连恢复） | ⚠️ | final_chunk 增量 + seq 序号 + X-Run-Id 断连查询 ✅；真实模型 token 流 ⛔（环境） |
| 04 | 业务工具（calculator/知识检索/DB 只读/网络按授权）+ 结构化错误 | ⚠️ | calculator 真执行、SQL 只读/SSRF/授权测试 ✅；知识检索实际检索无数据环境未跑 |
| 05 | Thread 隔离（≥20 并发会话不串） | ✅ | TestE2E::test_concurrent_sessions_isolated（20 并发独立会话） |
| 06 | Checkpoint 多轮连续 + 重启恢复 | ✅ | TestCheckpointRestart（落盘→新连接新实例恢复、多轮连续） |
| 07 | 多实例共享持久化后端 | ✅/⛔ | TestMultiInstance（双实例共享 DSN 续跑）✅；PG 多 Worker 实跑 ⛔（无 PG 环境） |
| 08 | 任务生命周期状态可区分 | ✅ | TestLifecycle 状态机 + TestBudgetAndLifecycle（limit_exceeded/超时/空结果 ok/取消 cancelled 不误报） |
| 09 | HITL（中断→审批→精确恢复/拒绝） | ✅ | test_hitl_interrupt_and_resume + demo 两阶段 + agent_approvals 落库 + 前端审批面板；重启后 HTTP 审批 ⛔（需部署） |
| 10 | 幂等副作用（不重复执行） | ✅ | TestIdempotentSideEffect（恰好一次/重复 resume 不重放/reject 不执行）+ 幂等键复用 |
| 11 | SubAgent（≥2 类专用 Agent 分派收回结果、工具隔离） | ✅ | test_subagent_delegation_and_result_collection（task 委派 researcher/data-analyst 收回结果）+ 工具收窄测试 |
| 12 | Skill（加载/启用/执行/禁用 ≥1 实际 Skill） | ✅ | 3 个实际 SKILL.md；test_skill_files_injected_into_run_state（装载进运行态）+ test_skill_registry_disable |
| 13 | 大上下文（工具输出卸载、大文件） | ✅ | TestLargeContext（2MB 文件 + 2MB 工具输出 FilesystemMiddleware 卸载） |
| 14 | 模型差异（不支持 tool calling 显式报错） | ✅ | TestModelAdapter::test_no_function_call_rejected（ModelCapabilityError） |
| 15 | 安全（SSRF/路径/越权/TLS） | ✅ | TestToolPolicy（7 项）+ TestOwnership + TLS 修复；残留 1 处非 Agent 路径 verify=False TODO |
| 16 | 工作流 Agent 节点 | ✅/⛔ | TestWorkflowAgentNode（HTTP 端到端 agent_invoke→WF_DONE_42）✅；MySQL 实跑 ⛔ |
| 17 | 资源与回滚 | ✅/⛔ | TestResourceLeak + AGENT_RUNTIME_ENABLED 一键回滚 + 旧 memory 迁移方案（02-migration.md）✅；MySQL 升级实跑 ⛔ |
| 18 | 代码质量 | ⚠️ | pytest 119 passed + 3 skipped（3 项 skip=真实模型 gated）、vite build 通过、lint 基线明确；未强制格式化门禁 |

## 二、P0 需求实现对照（任务书 P0 拆解 1~13）

| P0 | 项 | 状态 | 落点 |
| --- | --- | --- | --- |
| 1 | 执行路径统一（/chat、/invoke、workflow agent_invoke） | ✅ | AgentRuntimeService + AGENT_RUNTIME_ENABLED 回滚开关 |
| 2 | 模型层 OpenAI 兼容 + 流式 + 能力检测 | ✅ | model_adapter.py |
| 3 | 工具层 schema 校验 + 按授权下发 + 执行前再校验 | ✅ | tools/adapter.py、tools/policy.py |
| 4 | 业务工具集成（计算器/知识/DB 只读/网络） | ⚠️ | 均接入；知识检索实跑无数据环境 |
| 5 | SubAgent ≥2 + 上下文隔离/工具收窄 | ✅ | subagents.py（researcher / data-analyst） |
| 6 | Skills Registry + ≥1 可禁用 Skill | ✅ | skills.py + skills/ 三个 SKILL.md |
| 7 | 事件层 12 类结构化事件 + 旧 SSE 兼容 + 断连恢复 | ✅ | events.py（含 §7.2 字段、seq）+ X-Run-Id + GET /runs/{id} |
| 8 | 观测性（Langfuse/OTel 预留 + 脱敏） | ✅ | telemetry.py（TelemetryExporter 协议 + LoggingExporter）+ redact_sensitive |
| 9 | HITL 请求→持久化→前端审批→恢复 | ✅ | interrupt_on + Command(resume) + agent_approvals + AgentChat.vue 审批面板 |
| 10 | 副作用幂等 | ✅ | 测试 TestIdempotentSideEffect + 幂等键 |
| 11 | 配置化预算（步数/工具/模型/超时→limit_exceeded） | ✅ | ExecutionBudget + ToolCallLimit/ModelCallLimit(exit_behavior=error) + asyncio.timeout → limit_exceeded（TestBudget/TestBudgetAndLifecycle） |
| 12 | 迁移与回滚（旧 SQLite memory 处理） | ✅ | 02-migration.md（只读备份+幂等导入+游标续传+回滚）+ 回归基线 |
| 13 | 测试（确定性 + 集成 + 并发隔离 + 真实模型路径） | ✅/⛔ | 119+1；真实模型路径 gated ⛔（环境无模型服务） |

## 三、交付物清单

- 代码：`backend/app/services/agent_runtime/`（context/events/factory/model_adapter/persistence/backends/subagents/skills/lifecycle/**telemetry**/service + tools/{policy,adapter}）
- 数据模型：`agent_runs`、`agent_approvals`（alembic `b7c8d9e0f1a2`，离线 DDL 已验）
- API：/chat（X-Run-Id）/invoke（run_id 字段）切换；`GET /agents/runs/{run_id}`、`POST .../cancel|resume|approve`
- 前端：AgentChat.vue 审批面板（批准/拒绝→恢复流）
- 配置：config.py AGENT_* 段（含回滚开关 AGENT_RUNTIME_ENABLED、预算/超时、HITL、skills、telemetry）
- 演示：`backend/scripts/demo_runtime.py`（无 DB/模型依赖，HITL 两阶段 + checkpoint 恢复 + SSE seq）
- 文档：docs/agent-runtime/00-baseline、01-architecture、02-migration、03-operations、04-validation + 本文件
- 测试：
  - `tests/conftest.py`（临时 SQLite/内存 checkpoint 隔离）
  - `tests/test_agent_runtime.py`（36 项：事件/策略/适配/生命周期/E2E/Harness/Budget/Telemetry）
  - `tests/test_agent_runtime_persistence.py`（8 项：重启恢复/多实例/幂等副作用/大上下文/资源泄漏）
  - `tests/test_api_smoke.py`（10 项：HTTP 聊天流/越权/工作流节点/预算生命周期）
  - `tests/test_agent_runtime_e2e.py`（真实模型 gated 3 项：多步 tool calling / HITL 中断恢复 / 大上下文卸载）
- 依赖锁定：deepagents==0.7.34、langgraph==1.2.20、langchain==1.4.9、langgraph-checkpoint-sqlite==3.1.3、
  **bcrypt==4.0.1**（passlib 1.7.4 兼容）、**email-validator**（EmailStr 依赖，缺失曾致 ImportError）

## 四、测试结果（2026-11-09）

- `cd backend && python -m pytest tests/ -q` → **119 passed, 3 skipped**（3 项 skip 均为真实模型 gated E2E，环境无模型服务：
  已探测 localhost:3000/11434/8000/1234 无响应）。
- `python scripts/demo_runtime.py` → 多步工具调用 + HITL 中断→批准→精确恢复 + SSE 事件含 seq（04-validation.md §2）。
- `npx vite build` → 通过（vue-tsc 4 处 VxeTable `DefaultRow` 无关旧告警，记录为基线）。
- `alembic upgrade head --sql` → 离线生成 MySQL DDL（含 agent_runs/agent_approvals）；MySQL 3306/3307 均 10061 拒绝连接（环境阻塞证据见 04-validation.md §3）。

## 五、未完成项与风险（如实记录）

1. **真实模型 E2E ⛔**：本环境无 OpenAI 兼容模型服务；配 `AGENT_E2E_*` 后跑 `tests/test_agent_runtime_e2e.py`。
2. **MySQL 真实部署实跑 ⛔**：3306/3307 均拒绝连接；迁移链路以 alembic 离线 DDL 验证，部署后 `alembic upgrade head` 复验。
3. **PG 多 Worker ⛔**：`langgraph-checkpoint-postgres` 待装（03-operations.md 有切换步骤）。
4. 知识检索真实检索端到端未跑（无知识库数据环境）；重启后 HTTP 审批全流程待部署复验。
5. 审批超时无自动 TTL（人工处置/cancel）；工具副作用幂等需业务工具配合（02-migration.md）。
6. knowledge_service.py 1 处 verify=False 标注 TODO（非 Agent 执行路径，阶段二统一）。
7. 旧 engine.py 及其依赖保留作回滚（AGENT_RUNTIME_ENABLED=false），阶段二确认稳定后移除。
8. 未强制 ruff/black 格式化门禁（如需 CI 门禁另配）。

## 六、下一阶段接口

- 观测：telemetry.py `TelemetryExporter` 协议已预留 Langfuse/OTel 接入点（接上即用，运行不受导出影响）。
- 评测：基于 run 事件流的轨迹采集。
- SubAgent 并发/深度预算（ExecutionBudget.max_subagent_depth 预留字段）。
- Skill 市场与 UI（Registry 已就绪）。
- 工作流引擎 LangGraph 化（本阶段仅保证 Agent 节点走统一 Runtime）。

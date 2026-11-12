# 01-architecture - 架构分层、数据流、事件、持久化与安全

> 阶段一交付文档之一（任务书 §9.2）。版本基线：deepagents 0.7.34 / langgraph 1.2.20 /
> langchain 1.4.9 / langchain-core 1.7.6 / langchain-openai 1.7.4 /
> langgraph-checkpoint-sqlite 3.1.3 / openai 3.46.0（2026-11-09 实测兼容组合）。

## 1. 分层职责

| 层 | 实现 | 位置 |
| --- | --- | --- |
| DeepAgents Harness | `create_deep_agent`：规划、上下文压缩、文件工具、SubAgent、Skills、HITL | `backend/app/services/agent_runtime/factory.py` |
| LangChain | ChatModel / StructuredTool / Middleware 统一抽象 | `model_adapter.py`、`tools/adapter.py` |
| LangGraph | 图执行、State、Checkpointer、Stream、Interrupt/Resume | `persistence.py`、`service.py` |
| 平台业务层 | Run 生命周期、权限策略、事件/SSE 适配、可观测、API、工作流接入 | `lifecycle.py`、`tools/policy.py`、`events.py`、`telemetry.py`、`api/v1/agents.py` |

## 2. 核心执行链路

```text
Vue AgentChat.vue / POST /api/v1/agents/{id}/chat / workflow _exec_agent_invoke
        │
FastAPI（鉴权 + 所有权校验）
        │
AgentRuntimeService.start_run / stream_run / run_to_completion     service.py
        ├─ RunRegistry：run_id/thread_id/幂等键/状态机             lifecycle.py
        ├─ ToolPolicy：授权/SSRF/路径/沙箱/HITL 集合                tools/policy.py
        ├─ ToolAdapterFactory：BUILTIN_TOOLS -> StructuredTool（事件+策略内联）tools/adapter.py
        ├─ build_chat_model：ModelProvider/ModelConfig -> ChatOpenAI（AT-14 能力检测）model_adapter.py
        ├─ build_agent -> create_deep_agent(...)                   factory.py
        │      middleware: TodoList / ToolCallLimit / ModelCallLimit（+ 内置 Summarization）
        │      subagents: researcher / data-analyst（工具收窄）      subagents.py
        │      skills: /skills/（StateBackend files 注入）           skills.py
        │      backend: Composite(StateBackend + /workspace/ Filesystem) backends.py
        │      interrupt_on: 高危工具（HITL）
        ├─ Checkpointer：memory/sqlite/postgres（AGENT_CHECKPOINT_DSN）persistence.py
        ├─ telemetry：run/tool 记录导出钩子（预留 Langfuse/OTel）    telemetry.py
        └─ astream(updates+messages) -> RuntimeEvent -> EventBus
                 └─ LegacyEventAdapter -> 旧 SSE 契约（data: {json}，含 seq）events.py
```

## 3. 建模边界（四者不混用）

- `Agent`（agents 表）：可配置模板（模型、工具、提示词、预算）。
- `AgentSession`（agent_sessions 表）：用户可见会话索引与历史投影（`messages` JSON）。
- `Run`（agent_runs 表，新增）：一次执行实例（run_id=uuid4）。审批记录在 agent_approvals 表。
- LangGraph `thread_id` = `agent-{agent_id}-session-{session_id}`：持续执行状态标识。

**执行真相来源** = LangGraph checkpoint（thread 状态）；`AgentSession.messages` 仅作历史展示投影，
旧会话数据保持可读（assistant 消息只取 final 文本进入模型上下文）。
任何运行资源查询以 user 作用域（无租户概念时以 user_id 代）+ agent_id + session_id/run_id 授权隔离。

## 4. 组装决策记录

1. **不做跨 run 的编译图缓存**：工具闭包绑定 run 级上下文（db session、事件总线、预算计数），
   缓存会造成跨 run 状态泄漏。`build_agent` 每 run 组装一次（毫秒级）。
2. **`create_deep_agent` 内置工具（ls/read_file/write_file/edit_file/glob/grep/execute/task）无法经
   `tools` 参数移除**（官方 API 只增不减）。`execute` 在非沙箱 backend 上返回错误（DeepAgents 内置行为），
   平台级禁用再由 `ToolPolicy.check_exec_allowed` 兜底（AGENT_SANDBOX_MODE=disabled 时拒绝 python_exec 等）。
3. **模型增量过滤**：`stream_mode=["messages"]` 中只取 `metadata["langgraph_node"]=="model"` 的
   模型增量作为可见文本（已实测节点名为 `model`），子代理/工具节点的内部文本不进入最终回答。
4. **工具失败=显式状态**：适配层捕获异常 → `TOOL_FAILED` 事件 + 错误文本回传模型，
   不从字符串中搜索"失败"判定，也不让单个工具异常炸掉整个 run（策略拒绝除外，直接终止）。
5. **大输出卸载**：DeepAgents FilesystemMiddleware 对超阈值工具输出自动卸载到虚拟文件系统，
   上下文只留"Tool result too large ... saved in the filesystem at {path}"+预览（AT-13 实测）。

## 5. 与旧内核的关系

- 旧 `engine.py`（手写 ReAct）不再位于生产执行路径：`/chat`、`/invoke`、工作流 agent_invoke
  统一走 `AgentRuntimeService`（开关 `AGENT_RUNTIME_ENABLED`，见 02-migration.md）。
- 旧事件契约由 `LegacyEventAdapter` 保持字节级兼容（`data: {json}` 行），前端仅增量添加审批 UI。

## 6. 运行事件（RuntimeEvent，events.py）

最小字段对齐任务书 §7.2：

```json
{
  "event_id": "...", "event_type": "tool_started", "run_id": "...",
  "thread_id": "agent-1-session-2", "agent_id": 1,
  "timestamp": "2026-11-09T00:00:00Z", "sequence": 12,
  "status": "running", "payload": { "tool": "...", "input": { } }, "trace_id": "..."
}
```

- `sequence`：EventBus 发布时赋值的单调序号，客户端可检测丢事件/乱序；
- `thread_id/agent_id/status/trace_id`：EventBus 绑定 run 上下文后自动补齐；
- 事件数据经 `redact_sensitive` 脱敏：api_key/token/password 等置 `***`，长文本截断 1500 字符。

事件类型：`run_started / plan_updated / model_delta / tool_started / tool_completed /
tool_failed / subagent_started / subagent_completed / approval_required /
run_waiting_approval / run_completed / run_failed / run_cancelled`（共 12 类）。

## 7. 旧 SSE 兼容映射（LegacyEventAdapter）

线上格式（与旧前端逐字段兼容）：`data: {json}\n\n`，json 为扁平对象含 `type` 与自增 `seq`：

| RuntimeEvent | 旧事件 | 字段 |
| --- | --- | --- |
| plan_updated | thought | content="任务计划：..." |
| tool_started | action | tool, input |
| tool_completed / tool_failed | observation | tool, content |
| model_delta（首个前插 final_start） | final_chunk | content |
| approval_required | thought（附 approval 字段） | content, approval{run_id,actions} |
| run_completed | final + done | content=全部可见模型文本拼接；done.status=completed |
| run_failed | error + done | content=错误信息；done.status=failed/limit_exceeded |
| run_cancelled | error + done | done.status=cancelled |
| run_waiting_approval | done | done.status=waiting_approval（非终态，SSE 正常关闭） |

最终文本定义：**主模型节点全部可见增量拼接**（不含工具/子代理内部文本）。

## 8. 流式、断连与重连语义

- `/chat` SSE 响应头：`X-Session-Id`（保留旧契约）+ `X-Run-Id`（新增，运行创建后先行返回）。
- SSE 断连后执行**继续**（run 在服务端 asyncio task 中推进，checkpoint 保存状态）。
- 客户端重连/查询：`GET /api/v1/agents/runs/{run_id}` 返回状态与待审批动作（含 seq 可对账）。
- 去重：`/chat` 支持 `idempotency_key`（RunRegistry 按键复用未终态运行，防重复提交）。
- 取消：`POST /api/v1/agents/runs/{run_id}/cancel`（区分用户取消/模型失败/工具失败/等待审批/正常完成/空结果完成）。

## 9. 审批恢复流

`POST /api/v1/agents/runs/{run_id}/resume`（或 `/approve`）body:
```json
{"decisions": [{"type": "approve" | "reject", "message": "可选备注"}]}
```
响应为同契约 SSE 流（恢复后继续 model_delta/tool_*/final/done）。
拒绝后不执行被审工具（AT-09 实测）；重复 resume 不重放外部副作用（AT-10 实测）。

## 10. 持久化、线程与恢复语义

### 10.1 Checkpointer 配置（AGENT_CHECKPOINT_DSN）

| DSN | 实现 | 适用 |
| --- | --- | --- |
| `memory`（默认） | InMemorySaver | 本地/测试（进程重启丢状态） |
| `sqlite:///./data/agent_checkpoints.db` | AsyncSqliteSaver（自动 setup 建表） | 单实例持久化 |
| `postgresql+asyncpg://...` | AsyncPostgresSaver（langgraph-checkpoint-postgres，需另装） | 生产/多实例（推荐） |

任务书要求生产持久化用官方 PostgreSQL 后端：`persistence.py` 已实现 postgres 分支，
部署时安装 `langgraph-checkpoint-postgres` 并配置 DSN；多实例共享同一 PG 即可跨实例恢复。
禁止把 memory/sqlite 当企业级最终方案。跨线程长期记忆 Store：`AGENT_STORE_DSN`（memory/postgres，空=不启用）。

### 10.2 ID 约定

- `run_id`：uuid4 hex，agent_runs.id；`thread_id` = `agent-{agent_id}-session-{session_id}`。
- checkpoint 键 = thread_id；同一会话多轮连续 ⇒ 同一 thread 不断追加 checkpoint。
- 幂等键：`idempotency_key`，RunRegistry 内未终态即复用 + agent_runs 索引列。

### 10.3 恢复语义（SDK 级，已验证）

- `agent.ainvoke(input, config)` 每步落 checkpoint；`aget(config)` 取 snapshot
  （`get_thread_state` API 暴露 checkpoint_id/next/pending_writes）。
- HITL 中断：checkpoint 保留待执行工具调用；恢复用 `Command(resume={"decisions":[...]})`
  + 同 thread config **精确续跑**（不重放已执行工具）。
- 进程重启后恢复：同 thread_id 的下一次 invoke 自动从最近 checkpoint 继续（AT-06 实测）；
  多实例共享持久化后端可互相查询/恢复（AT-07 实测，sqlite 多连接验证；postgres 待部署验证）。
- **副作用边界**：checkpoint 只保证执行状态恢复，**不保证工具副作用恰好一次**。对有外部副作用的
  工具：① 默认 HITL 审批；② 审批记录落 agent_approvals 作人工回执；③ 幂等键由业务工具入参携带，
  恢复后重试需业务幂等（AT-10 实测重复 resume 不重放）。

### 10.4 Run 状态机（lifecycle.py）

```text
pending → running → completed / failed / cancelled / limit_exceeded
running → waiting_approval → running（恢复）/ cancelled / failed
```

- 单向转移 + 白名单（`ALLOWED_TRANSITIONS`），非法转移抛 `InvalidTransition`；并发安全（状态加锁）。
- 终态不可再转移；状态落 agent_runs（status/error/final_text/summary/finished_at）。
- 预算超限（步数/工具/模型次数/墙钟超时）→ `limit_exceeded`，与 `failed` 区分；
  空结果正常完成 ≠ 错误。
- 取消：运行中 = asyncio task cancel（CancelledError → run_cancelled）；waiting_approval = 直接置 cancelled。

## 11. 安全与权限

### 11.1 工具授权（tools/policy.py）

- **执行前强制检查**：模型端隐藏 ≠ 服务端授权。ToolPolicy 仅允许 Agent 配置中启用的工具集合，
  适配层 `_execute` 每次调用先过 `check_tool_allowed`，未授权 → ToolPolicyError + TOOL_FAILED。
- 风险分级：READ / WRITE / NETWORK / EXECUTE / DANGEROUS（TOOL_RISK 表）；默认最小授权。
- HITL 默认工具集：python_exec / http_post / webhook_notify / send_email_tool / delete_file
  （`interrupt_on` 审批后才执行；AGENT_ENABLE_HITL=false 可关）。
- SQL 只读守卫：db_query 拒绝 insert/update/delete/drop/alter/truncate/create/grant/revoke。
- 预算守卫：工具调用次数超预算立即 TOOL_FAILED + 终止（不静默继续）。

### 11.2 SSRF / 网络（check_url）

- 仅 http/https scheme；拦截回环、链路本地、保留、私网、云元数据（169.254.169.254 等）。
- DNS 解析后按 IP 判定（残余风险：解析与请求间 TOCTOU，生产建议网关层再加 egress 策略）。
- `AGENT_NETWORK_ALLOWLIST`：配置后仅允许名单内主机，且名单内主机**刻意放行内网**
  （内网受信任目标）；未配置时全网放行但保留内网/元数据拦截。
- `AGENT_NETWORK_BLOCKED_CIDRS`：额外封禁网段。

### 11.3 文件路径守卫（check_path）

- 仅允许 `AGENT_FILES_ROOT/{user}/{agent}/{session}` 工作区；拒绝绝对路径与 `..` 穿越。
- FilesystemBackend `virtual_mode=True` + `max_file_size_mb`（默认 10MB）；
  工作区根目录按 user/agent/session 三级隔离，绝不指向宿主机根目录。

### 11.4 TLS（已修复 verify=False）

- 默认 `verify=True`（AGENT_TLS_VERIFY）；内网自签证书用 `AGENT_CA_BUNDLE` 指定 CA。
- 已修复 8 处硬编码 `verify=False`（清单见 00-baseline.md §3）；
  残留 knowledge_service.py 1 处（非 Agent 执行路径，TODO 阶段二）。

### 11.5 沙箱（AGENT_SANDBOX_MODE）

| 模式 | 行为 |
| --- | --- |
| `restricted`（默认） | 高危执行工具（python_exec/execute/delete_file）需显式启用并过 HITL |
| `disabled` | 高危执行工具直接 ToolPolicyError 拒绝（内网/生产默认） |
| `external` | 预留：接入外部隔离沙箱（Docker/Firecracker）后放开 |

DeepAgents 内置 `execute` 在非沙箱 backend 下自身返回错误（官方行为），
python_exec 类工具由策略层禁用 —— **不以 Prompt 提示代替隔离**。

### 11.6 越权修复（api/v1/agents.py）

- `_get_or_404` 增加所有权校验：非公开 Agent 仅所有者可见/可改/可删（读取/删除/聊天/调用全覆盖）。
- `/chat` 会话查询、`delete_session` 增加 `user_id == current_user.id` 约束。
- Run 级资源查询（`/runs/{id}`、cancel、resume/approve）校验 `run.user_id == current_user.id`。
- 事件输出经 `redact_sensitive`：密钥字段置 `***`、长参数截断，审批记录只存脱敏摘要。

## 12. 可观测（telemetry.py，P0-H）

- `TelemetryRecord`：kind(run/tool) + name + status + fields + duration_ms + error + attributes；
  fields 含 request_id / trace_id / run_id / thread_id / agent_id / tenant_id / user_id /
  session_id / model_id（RuntimeContext.to_log_fields 提供）。
- `TelemetryExporter` 协议为 **Langfuse / OpenTelemetry 预留接入点**：
  实现 `export(record)` 后 `telemetry.add_exporter(...)` 注册；默认 LoggingExporter 落结构化日志。
- 挂钩点：`service._drive`（run 级 started/终态+耗时+summary）、`tools/adapter._execute`
  （tool 级 completed/failed/rejected+耗时）；导出异常绝不影响执行。

## 13. SubAgent / Skills / 文件 Backend 要点

- **SubAgent**（subagents.py）：researcher（资料检索/网页工具）、data-analyst（calculator/data 工具）；
  工具集合=父工具收窄交集，提示词按角色专用；派生深度/并发/预算由 ExecutionBudget 与
  DeepAgents 默认隔离语义控制（上下文隔离）。实测见测试 `test_subagents_narrowed_tools`。
- **Skills**（skills.py）：SkillRegistry（元数据/版本/启用范围/来源/访问控制），内置
  chart-report / meeting-minutes 两个 Skill，`AGENT_DISABLED_SKILLS` 禁用；
  经官方 Skills 装载（StateBackend files 注入）。
- **文件 Backend**（backends.py）：Composite(StateBackend + `/workspace/` FilesystemBackend
  virtual_mode)；skills/subagent/worktree 放 StateBackend，/workspace 持久落盘且按
  user/agent/session 隔离。

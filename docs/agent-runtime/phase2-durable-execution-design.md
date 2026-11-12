# Phase 2 Durable Execution 架构说明

> 对应任务书：`docs/AI-GeneralPlat_AgentRuntime_Phase2_DurableExecution_TaskBook.md`
> 范围：FR-01 ~ FR-10 的可运行代码实现。本文描述**实际落地的设计与机制**，不是目标设想。

---

## 1. 总体目标

当服务进程、网络连接、Worker、模型请求或外部工具发生故障时，平台能够明确知道：

- 任务执行到了哪里（Run 状态 + 事件日志）；
- 哪些副作用已经发生（Tool Execution Ledger）；
- 哪些动作可以安全恢复（幂等键 + fencing token）；
- 哪些动作必须人工核对（`unknown` / `needs_reconciliation`）。

**不承诺"恰好一次"外部副作用**（见 §10 不变量）；只承诺"至少一次 + 可检测重复 + 重复可控"。

## 2. 状态源职责（原则 A/B）

| 状态源 | 承载内容 | 事实来源范围 |
|---|---|---|
| 平台数据库（`agent_runs` 等） | Run 生命周期、租约、幂等、审批、账本、事件 | **平台 Run 状态的唯一事实来源** |
| LangGraph Checkpoint | 图执行状态（消息通道、图节点进度） | 仅图内状态恢复，不承载业务 Run 状态 |
| `agent_sessions.messages` | 面向用户的会话展示历史 | 展示层副本，可从事件日志重建 |
| `agent_run_events` | 追加式事件日志 | SSE 回放、断线续传、final_text 重建 |
| `agent_tool_executions` | 副作用账本 | "副作用是否发生"的唯一判据 |

Checkpoint 与业务 Run 分工：`run.status` 永远以数据库为准；Checkpoint 缺失/损坏只影响图内状态（→ F-16 处理），不影响 Run 状态判定。

## 3. 数据模型与迁移

迁移链（全部为独立新增 revision，未修改历史迁移语义）：

```
9ce3d801b702 (initial)
  → e5f6a7b8c9d0  基线修复：补齐空库缺失的 agents 等基础表
  → a1b2c3d4e5f6  database_connections（改为 batch 模式兼容 SQLite）
  → b7c8d9e0f1a2  agent_runs / agent_approvals 基础表
  → d4e5f6a7b8c9  Phase 2 durable execution（本阶段主迁移）
  → f1a2b3c4d5e6  agent_run_events 增加 thread_id（head）
```

### 3.1 `agent_runs` 扩展（FR-01/02/03/07/10）

| 列 | 用途 |
|---|---|
| `request_hash` | 幂等键命中时校验请求体一致（不一致 → `IdempotencyConflict`） |
| `input_payload` / `agent_config_snapshot` | 恢复执行所需的输入与配置快照 |
| `started_at` / `worker_id` / `heartbeat_at` | 执行归属与活性 |
| `lease_expires_at` / `lease_version` | 租约与 fencing token（原则 C） |
| `attempt` | 执行尝试计数（每次 Claim +1） |
| `last_event_seq` | 事件序号分配游标（并发安全，见 §7） |
| `recovery_error` | 恢复失败原因（人工核对入口） |
| 唯一约束 `uq_agent_runs_idem(user_id, idempotency_key)` | 幂等创建的数据库兜底 |

### 3.2 `agent_approvals` 扩展（FR-04/06）

`action_id`（稳定动作身份）、`tool_call_id`、`args_hash`、`action_payload`、`decision_status`、`updated_at`；
唯一约束 `uq_agent_approvals_action(run_id, action_id)` —— 重复提交同一审批天然幂等。

### 3.3 `agent_tool_executions`（FR-05，新增表）

`run_id + tool_call_id` 唯一（`uq_tool_exec_run_call`）。核心列：`args_hash`、`args_snapshot`、`idempotency_key`、
`status`、`attempt_count`、`result_payload`、`error`、`external_operation_id`、`worker_id`、`lease_version`、
`started_at/finished_at`。索引：`(run_id, status)`、`(status, updated_at)`。

### 3.4 `agent_run_events`（FR-07，新增表）

`run_id + sequence` 唯一（`uq_run_event_seq`）；`event_type`、`payload`（JSON，文本型事件不截断）、
`thread_id`（f1a2b3c4d5e6 补充）。索引 `(run_id, sequence)`。

## 4. 核心机制

### 4.1 Run 幂等创建（FR-01）

- `POST /agents/{agent_id}/chat` 接受可选 `Idempotency-Key` 请求头；
- `RunRepository.create_run`：同 `(user_id, idempotency_key, request_hash)` → 复用原 Run（返回原 ID 与状态）；
  `request_hash` 不同 → 抛 `IdempotencyConflict`；无幂等键 → 每次新建；
- 数据库唯一约束兜底：绕过仓库的重复插入被 `IntegrityError` 拒绝（`test_db_enforces_idempotency_uniqueness`）；
- 创建是**单事务**：提交前崩溃不留半成品（F-01）；提交后崩溃留下 `pending` Run，可被任何 Worker 发现（F-02）。

### 4.2 Worker Claim / Lease / Fencing（FR-02，原则 C）

- `RunRepository.claim`：条件更新（`pending` 或租约过期的 `running`）→ `worker_id`、`lease_expires_at`、
  `lease_version += 1`、`attempt += 1`；**同一行只有一个赢家**（F-12）；
- `heartbeat`：校验 `worker_id + lease_version` 后续租；不匹配拒绝；
- 所有终态提交（`update_status`）与账本写入都携带 `lease_version` 校验 —— 租约丢失/释放后旧 Worker 的
  fencing token 失效，不能再提交状态或副作用（F-13）；
- `claim_next`：跳过 `waiting_approval` / `needs_reconciliation`（FR-02.9，暂停态不被误领），
  可领取 `pending` 与租约过期的 `running`（孤儿恢复，F-02/F-03）；
- `RunWorker`：常驻协程扫描 + 心跳线程 + 优雅关闭（`shutdown_grace` 内等待在途 Run，随后释放租约使其可被接管）。

### 4.3 进程重启恢复（FR-03）

恢复分类与动作：

| 分类 | 判定 | 动作 |
|---|---|---|
| 可安全执行 | `pending` / 租约过期 | 重新 Claim（attempt+1）执行 |
| 审批挂起 | `waiting_approval` | 保持，不自动恢复；审批从数据库读取（F-10） |
| 副作用不确定 | 账本 `unknown` / `needs_reconciliation` | **禁止自动重放**（F-07/F-08），转人工核对 |
| Checkpoint 缺失 | 图状态不可用 | 明确失败并记 `recovery_error`，不从头重放有副作用的任务（F-16） |

恢复时若账本中该 `tool_call_id` 已 `succeeded`，直接返回已存 `result_payload`，不再执行副作用（F-09）。

### 4.4 HITL 审批持久化（FR-04/06）

- 中断即落库：`agent_approvals` 行（含 `action_id`、`action_payload`、`decision_status=pending`），
  Run 转 `waiting_approval`；进程崩溃后信息仍可从数据库读取（F-10）；
- 决策幂等：同 `action_id` 重复提交 → 幂等返回首次结果，不重复恢复、不重复执行工具（F-11）；
- 冲突语义：不同 `action_id` 或已决状态下再决策 → 明确报冲突（`ApprovalConflict`），不做静默覆盖；
- 恢复 = 同 Thread（`make_thread_id`）`Command(resume=...)` 继续图执行；审批与工具账本通过
  `tool_call_id` 协同：批准后账本从 `prepared` 进入实际执行。

### 4.5 Tool Execution Ledger（FR-05，原则 D/E）

执行流程（先登记，再执行，再确认）：

```
prepared（登记 tool_call_id/args_hash/idempotency_key）
  → executing（带 fencing token）
  → succeeded（保存 result_payload，不可变） / failed（可安全重试类）
  → unknown（外部副作用结果不确定）→ needs_reconciliation（人工核对）
```

- `tool_call_id` 来自 LangGraph 工具调用 ID，跨 Checkpoint 恢复稳定；
- 幂等键策略（原则 E）：
  - 只读工具 / 本地工具 → 失败可重试（F-05）；
  - 外部副作用工具且**有下游幂等能力** → 用 `idempotency_key` 查询/重试，复用同一键（F-07 前半）；
  - 外部副作用工具**无下游幂等能力**，执行后结果未知 → 标 `unknown`，**不自动重放**（F-07 后半 / F-08）；
- `succeeded` 状态不可变；`test_ledger_succeeded_is_immutable`、`test_ledger_no_unsafe_after_success` 验证
  成功后不允许覆盖/不安全迁移；
- 工具执行器（`ToolExecutor.execute`）新增 `raise_on_error`：账本路径抛真实异常以区分 `failed`/`unknown`，
  而不是把错误吞成字符串。

### 4.6 Run Event 持久化与 SSE 回放（FR-07）

- 事件先持久化（`agent_run_events`）再通知：`sequence` 由 `agent_runs.last_event_seq` 条件递增分配，
  唯一约束 `uq_run_event_seq` 兜底，并发 append 不重复（`test_event_sequence_concurrent_appends_unique`）；
- EventBus 内存队列满 → 持久化事件不丢，客户端可通过回放/轮询补齐（F-15）；
- SSE 支持 `Last-Event-ID` 头与 `after_seq` 参数：断线重连从持久化日志继续，无丢失无重复（F-14，T-08）；
- 保留策略：`AGENT_EVENT_RETENTION_DAYS`，Worker 周期清理**终态且超期** Run 的事件；
  进行中/暂停中的 Run 永不清理；
- payload 脱敏（密钥遮蔽）但**文本型事件（MODEL_DELTA / RUN_COMPLETED）不截断**，否则回放丢文本。

### 4.7 Session / Checkpoint 消息一致性（FR-08，F-18）

**单一主策略：稳定消息 ID + 每轮确定性重播种。**

- 每条消息携带稳定 ID（`stable_message_id`：有 ID 用原 ID；否则 `sha256(namespace|index|role|content)` 派生）；
- 注入负载以 `RemoveMessage(REMOVE_ALL_MESSAGES)` 打头 + 完整 Session 历史（带稳定 ID），
  通过 deepagents messages reducer 的按 ID 替换语义**整通道确定性重播种**；
  这同时解决：模型生成消息（随机 UUID）与 Session 副本身份无法对齐、旧线程历史无 ID 的兼容问题；
- 挂起恢复（`Command(resume)` / payload 为空）**不重播种**，Checkpoint 内状态原样续跑；
- FR-08.1：用户消息在 Run 创建前可靠持久化（`/chat` 追加后立即写 Session，失败则 500 不启动运行），
  消除"运行已开始但用户消息未落库"的窗口；
- 验证：T-09 三轮对话 + 每轮模拟服务重启（清空内存注册表），Graph state 中每条消息恰出现 1 次；
  会话历史严格 `user/assistant` 交替；同历史重试注入不产生重复。

### 4.8 最终结果收集与事件适配解耦（FR-09）

- **唯一权威收集点**是执行侧 `_consume_stream()`：`_extract_text()` 从消息提取文本（str 直取；
  list 按 text 块/纯字符串块拼接），累积到 `run.final_text_parts`；adapter 只做协议转换、不再持有结果；
- 完成顺序（防竞态）：**先持久化最终结果（DB commit）→ 写回 Session → 最后发布 RUN_COMPLETED**；
- 消费者是否消费 stream、消费多晚，都不影响 DB 中 `final_text` 的正确性（T-10 验证：不消费 stream 直接
  等终态，DB 与后到的 `final` 事件内容一致）；
- 跨挂起/重启：进入 `final_text_parts` 为空时从事件日志（MODEL_DELTA payload）重建片段。

### 4.9 模型调用预算与执行步数（FR-10）

- `ModelUsageCallback(BaseCallbackHandler)`：`on_chat_model_start / on_llm_start` 按 `run_id` 去重计数
  → `model_calls += 1`（一次模型请求 = 1，与流式 chunk 数无关）；
- `tokens_generated`：优先取 usage 元数据（`usage_metadata.output_tokens` / `llm_output.usage` 的
  `output_tokens`/`completion_tokens`），缺失时用流式 token 事件计数兜底；
- `model_node_runs` / `steps`：在 `_consume_stream` 的 updates 分支，主图 `"model"` 节点完成时各 +1；
- 摘要写入 `run.summary`：`tool_calls / model_calls / model_node_runs / tokens_generated / steps / elapsed_seconds`；
- 预算耗尽：`ToolPolicy` 超限抛 `BudgetExceededError`，服务层映射为 **`limit_exceeded`（非 `failed`）**
  （T-11 验证：`AGENT_MAX_MODEL_CALLS=1` + 两请求脚本 → `limit_exceeded`）。

## 5. 与既有机制的关系

- **保留**：DeepAgents Agent Harness、LangGraph Graph 执行与 Checkpointer、现有 Telemetry/Langfuse、
  `/agents/{id}/chat`、`/runs/{id}`、`/runs/{id}/cancel`、`/runs/{id}/resume` 等 API 形态（仅增字段）；
- **变化**：Run 生命周期由平台数据库接管；Worker 协调由租约/Claim 接管；工具副作用由账本接管；
  事件投递由持久化日志 + 回放接管；Session 一致性由稳定 ID 重播种接管；
- `AGENT_RUNTIME_ENABLED=False` 可整体回退旧引擎路径（回滚开关）。

# Agent Runtime Phase 2 Durable Execution 改造任务总结报告

> 任务来源：`docs/AI-GeneralPlat_AgentRuntime_Phase2_DurableExecution_TaskBook.md`
> 完成日期：2026-11-12
> 最终验证：105 passed / 0 failed / 0 skipped（4 个测试文件，干净环境 10.29s）

---

## 1. 任务背景与目标

在 Phase 0~5 已完成（持久化 Repositories、Worker 租约、工具账本、HITL 审批、事件持久化）的基础上，按任务书完成第二阶段 **Durable Execution** 改造的收尾部分与整体交付，使平台在**服务进程崩溃、网络断连、Worker 丢失、模型请求失败、外部工具不确定返回**等故障下能够：

1. 明确知道任务执行到哪里（Run 状态 + 事件日志）；
2. 明确知道哪些副作用已经发生（Tool Execution Ledger）；
3. 明确区分可安全恢复与必须人工核对的动作（幂等键 + fencing token + `unknown` 治理）；
4. **绝不伪造"恰好一次"**——只承诺"至少一次 + 可检测重复 + 重复可控"。

全程遵守任务书铁律：先审查后修改、独立迁移不改历史迁移语义、API 向后兼容（只加不改）、关键持久化异常向上抛出、不编造验证结果（未实测项如实标注）、不引入 Redis/Celery/Kafka、每阶段跑测试后再宣称完成。

## 2. 实施阶段总览

| 阶段 | 内容 | 验证门禁 |
|---|---|---|
| Phase 0 | 基线审查（R-01~R-12 风险清单） | `phase2-baseline-review.md` |
| Phase 1 | 数据模型 + Alembic 迁移 + Repositories（幂等创建/租约/账本/事件） | 25 项新增 + 54 项回归全过 |
| Phase 2 | Worker Claim/Lease/Heartbeat/Fencing + 优雅关闭 + 孤儿恢复 | 88 项回归全绿 |
| Phase 3 | Tool Execution Ledger 状态机 + 幂等键 + `unknown` 治理 | T-04/T-05 通过 |
| Phase 4 | HITL 审批持久化 + `action_id` 幂等/冲突语义 + 重启恢复 | T-02/T-03 通过，94 项全绿 |
| Phase 5 | 事件持久化 + 原子 sequence + SSE `after_seq`/`Last-Event-ID` 回放 + 保留策略 | T-08 通过，100 项全绿 |
| Phase 6 | 消息去重（FR-08）/ 最终文本竞态（FR-09）/ 模型计数（FR-10） | T-09/T-10/T-11 通过，103 项全绿 |
| Phase 7 | Idempotency-Key API 接线（§8.2）+ 交付文档 + 最终回归 | **105 项全绿** |

## 3. 关键改动详情（按需求项）

### 3.1 FR-01 Run 持久化与幂等创建
- `agent_runs` 扩展 11 列：`request_hash`、`input_payload`、`agent_config_snapshot`、`started_at`、`worker_id`、`heartbeat_at`、`lease_expires_at`、`lease_version`、`attempt`、`last_event_seq`、`recovery_error`；
- 唯一约束 `uq_agent_runs_idem(user_id, idempotency_key)` 数据库兜底（绕过仓库的重复插入被拒绝）；
- 同键同请求摘要 → 复用原 Run 返回原 ID 与状态；同键不同摘要 → `IdempotencyConflict`（API 层映射 409）；
- **API 接线**：`POST /agents/{id}/chat` 与 `/invoke` 接受可选 `Idempotency-Key` 请求头；幂等命中在**会话/消息变更之前**返回原 Run，不重复执行、不重复追加用户消息。

### 3.2 FR-02 Worker 租约 / Fencing
- `RunRepository.claim`：条件更新单赢家；每次 Claim 使 `lease_version += 1`（fencing token）且 `attempt += 1`；
- `heartbeat` 校验 worker_id + lease_version 后续租；不匹配拒绝；
- 所有终态提交与账本写入携带 fencing 校验——租约丢失/释放后旧 Worker 不能提交状态或副作用（F-13）；
- `claim_next` 跳过 `waiting_approval`/`needs_reconciliation`（暂停态不被误领），可领取 pending 与租约过期的 running（孤儿恢复）；
- `RunWorker`：常驻扫描 + 心跳 + 优雅关闭（grace 期内等待，随后释放租约可被接管）；随 FastAPI lifespan 启停。

### 3.3 FR-03 进程重启恢复
四分类处置：可安全执行（重新 Claim）/ 审批挂起（保持等待）/ 副作用不确定（`unknown`，禁止自动重放，转人工）/ Checkpoint 缺失（明确失败 + `recovery_error`，不从头重放有副作用任务）。恢复时账本已 `succeeded` 的工具调用直接返回已存结果（F-09）。

### 3.4 FR-04/06 HITL 审批持久化
- 中断即落库 `agent_approvals`（`action_id`、`args_hash`、`action_payload`、`decision_status`）；唯一约束 `uq_agent_approvals_action(run_id, action_id)`；
- 重复提交一致决策 → 幂等返回（不重复恢复/执行，F-11）；冲突决策 → `ApprovalConflict` 显式拒绝；非法决策 → `ApprovalInvalid`；
- 审批决策先持久化成功才继续执行（关键写失败向上抛）；恢复用 `Command(resume=...)` 续图，与账本经 `tool_call_id` 协同。

### 3.5 FR-05 Tool Execution Ledger
- 新表 `agent_tool_executions`（`(run_id, tool_call_id)` 唯一）；状态机 `prepared → executing → succeeded / failed / unknown → needs_reconciliation`；
- 执行流程"先登记、再执行、再确认"；`succeeded` 不可变（测试验证不可覆盖）；
- 幂等策略三分：只读/本地工具失败可重试；有下游幂等能力复用同一 `idempotency_key`；无下游幂等能力且结果未知 → `unknown` 阻断自动重放（F-07/F-08）；
- `ToolExecutor.execute(raise_on_error=)` 区分真实 `failed` 与不确定 `unknown`。

### 3.6 FR-07 事件持久化与 SSE 回放
- 新表 `agent_run_events`（`(run_id, sequence)` 唯一 + `thread_id` 列）；
- `sequence` 由 `agent_runs.last_event_seq` 条件递增原子分配，并发 append 不重复；
- 发布先持久化再通知内存队列；队列满仅丢通知不丢持久化事件（F-15）；
- SSE 支持 `Last-Event-ID` / `after_seq` 断线续传（F-14）；保留策略仅清理**终态且超期** Run 的事件；
- payload 密钥遮蔽但文本型事件（MODEL_DELTA/RUN_COMPLETED）不截断，回放保真。

### 3.7 FR-08 Session / Checkpoint 消息一致性（F-18）
- **稳定消息 ID + 每轮确定性重播种**：注入负载以 `RemoveMessage(REMOVE_ALL_MESSAGES)` 打头 + 完整 Session 历史（带稳定 ID，`sha256(namespace|index|role|content)` 派生），经 deepagents reducer 的按 ID 替换语义整通道重播种；
- 解决模型生成消息（随机 UUID）与 Session 副本身份不对齐、旧线程历史无 ID 兼容两类问题；
- 挂起恢复（`Command(resume)`）不重播种；FR-08.1 用户消息在 Run 创建前可靠持久化（保存失败 500 不启动运行）。

### 3.8 FR-09 最终文本竞态
- 唯一权威收集点在执行侧 `_consume_stream()`（`_extract_text()` 提取文本累积 `final_text_parts`），adapter 只做协议转换；
- 完成顺序防竞态：**先持久化最终结果 → 写回 Session → 最后发 RUN_COMPLETED**；
- 跨挂起/重启从事件日志（MODEL_DELTA payload）重建片段；消费者是否/多晚消费 stream 均不影响 DB 结果。

### 3.9 FR-10 模型计数与预算
- `ModelUsageCallback`：`on_chat_model_start`/`on_llm_start` 按 run_id 去重，一次模型请求 = 1（流式 chunk 不重复计数）；
- `tokens_generated` 优先 usage 元数据、流式 token 事件兜底；`model_node_runs`/`steps` 在主图 "model" 节点完成时递增；
- 摘要写入 `run.summary`；预算耗尽抛 `BudgetExceededError` → 映射 **`limit_exceeded`（非 `failed`）**。

### 3.10 其他配套改动
- `app/core/database.py`：SQLite 启用 WAL + 30s 锁等待（Worker/事件并发写不互锁）；
- `app/main.py`：lifespan 挂载 RunWorker 启动/优雅停止；
- `app/api/v1/workflows.py`：执行前提交事务释放写锁；
- 状态机新增 `limit_exceeded`/`needs_reconciliation` 转移；`AGENT_RUNTIME_ENABLED=false` 整体回退开关。

## 4. 数据库变更

**迁移链**（独立新增，head = `f1a2b3c4d5e6`）：

```
9ce3d801b702 (initial)
  → e5f6a7b8c9d0  空库基线修复（补齐缺失基础表）
  → a1b2c3d4e5f6  database_connections（改 batch 模式兼容 SQLite，语义不变）
  → b7c8d9e0f1a2  agent_runs / agent_approvals 基础表
  → d4e5f6a7b8c9  Phase 2 durable execution（主迁移）
  → f1a2b3c4d5e6  agent_run_events + thread_id（head）
```

- 修改表：`agent_runs` +11 列、`agent_approvals` +6 列；
- 新增表：`agent_tool_executions`、`agent_run_events`；
- 唯一约束 4 组：`uq_agent_runs_idem`、`uq_agent_approvals_action`、`uq_tool_exec_run_call`、`uq_run_event_seq`；
- **验证**：SQLite 实跑 `alembic upgrade head → 断言表/列/约束 → downgrade base → upgrade head` 全部通过；
- **如实披露**：历史迁移文件唯一例外修改为 `add_database_connections.py`（重接 revision 链线性化 + ALTER 改 batch 模式，语义不变，原因：空库升级链原本无法通过）。

## 5. API 兼容性

- `/agents/{id}/chat`、`/invoke`、`/runs/{id}`、`/runs/{id}/cancel`、`/runs/{id}/resume` 路径与既有字段**不变，仅新增可选字段/请求头**（`Idempotency-Key`、响应新增 `run_id`/`idempotent_reuse`/租约与预算摘要字段）；
- SSE 新增 `Last-Event-ID`/`after_seq` 续传，旧客户端不带参数行为不变；
- `AGENT_RUNTIME_ENABLED=false` 一键回退旧引擎。

## 6. 验证结果

### 6.1 总体结果

```
python -m pytest tests/test_agent_runtime.py tests/test_agent_runtime_persistence.py \
    tests/test_agent_runtime_durable.py tests/test_api_smoke.py -q

→ 105 passed, 0 failed, 0 skipped（2026-11-12）
```

| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| test_agent_runtime.py | 36 | 运行时/图/工具/E2E 回归 |
| test_agent_runtime_persistence.py | 8 | Checkpoint 重启恢复、多实例共享线程、副作用恰一次 |
| test_agent_runtime_durable.py | 47 | FR-01~10 主测试、T-01~11、故障注入、迁移 upgrade/downgrade |
| test_api_smoke.py | 14 | API 冒烟、SSE 回放、预算/超时、Idempotency-Key（2 项） |

### 6.2 集成测试 T-01 ~ T-11（全部通过）

| 编号 | 场景 | 关键断言 |
|---|---|---|
| T-01 | 正常多步工具调用 | 账本 tool_call_id/幂等键稳定 |
| T-02 | 服务对象重建后审批恢复 | 重启后从 DB 读审批并可恢复 |
| T-03 | 审批重复提交 | 幂等返回、冲突显式拒绝、非法拒绝 |
| T-04 | 工具成功后 Checkpoint 前崩溃 | 重放返回账本已存结果，不重复执行 |
| T-05 | 外部 POST 不确定结果 | 无幂等键 → `unknown` 阻断；有幂等键 → 复用同键 |
| T-06 | 多 Worker 并发 | 单赢家 Claim；两 Run 各恰执行一次 |
| T-07 | Lease 过期 | 旧 Worker fencing 失效，不写状态 |
| T-08 | SSE 断线回放 | after_seq/Last-Event-ID 续传无丢失无重复 |
| T-09 | Session/Checkpoint 不重复 | 3 轮×重启模拟：Graph state 消息各恰 1 次，会话严格 user/assistant 交替，重试注入不重复 |
| T-10 | 最终文本竞态 | 不消费 stream 时 DB final_text 仍完整权威 |
| T-11 | 模型计数与预算 | 3 chunk 单请求 = 1 次调用；预算触顶 → `limit_exceeded` |

### 6.3 故障场景 F-01 ~ F-18（全覆盖映射，F-16 部分覆盖）

F-01 创建崩溃、F-02 提交后崩溃、F-03 Claim 后崩溃、F-04 模型调用崩溃、F-05 只读工具崩溃、F-06 POST 前崩溃、F-07 响应未知、F-08 账本未提交、F-09 账本成功 Checkpoint 未写、F-10 HITL 崩溃、F-11 重复审批、F-12 双 Worker 竞争、F-13 租约过期旧 Worker 复活、F-14 SSE 断线、F-15 队列满、F-17 持久化失败上抛、F-18 重复注入——均有对应故障注入测试并符合预期。**F-16**（Checkpoint 损坏）仅覆盖账本兜底路径，位级损坏场景未测（已在未完成项标注）。

### 6.4 稳定性调查（如实记录）

开发过程中套件曾偶发"卡住不前"。经二分定位与进程核查，根因为**多个 pytest 进程并发争抢共享 SQLite 测试库**（遗留后台测试进程未清理时触发），并非用例或被测代码缺陷。清理并发进程后多轮循环（≥5 轮）运行稳定，捕获统计均为 105 passed / 0 failed。CI 应保证同一测试库上不并发执行本套件。

## 7. 已知限制与遗留风险

1. **MySQL / PostgreSQL 未实测**（本地无实例）：迁移用 `batch_alter_table` 兼容设计，上线前必须在生产同版本库预演 upgrade/downgrade；
2. **AsyncPostgresSaver / AsyncPostgresStore 路径未实测**（缺依赖/实例）；
3. **不承诺"恰好一次"外部副作用**：`unknown` 状态依赖人工核对 SOP 最终一致；
4. F-16 位级损坏、真实 NTP 漂移、长稳/负载测试未纳入本阶段；
5. `tokens_generated` 无 usage 元数据时为流式计数兜底，可能与计费口径有偏差；
6. `model_node_runs` 依赖 deepagents 图主节点名为 `"model"`（内部约定，内核升级需复核）；
7. 多实例跨进程 SSE 实时通知不保证（进程内通知 + 回放/轮询补齐）；
8. `agent_tool_executions` 不做自动清理（审计留痕，归档由 DBA 另行制定）。

## 8. 生产部署前人工确认清单

1. 生产同版本 MySQL/PG 上预演 `alembic upgrade head` / `downgrade`；
2. `AGENT_CHECKPOINT_DSN` 选型（生产建议 PostgreSQL）并确认持久卷；
3. 盘点副作用类工具：下游幂等键支持与透传方式；
4. `needs_reconciliation` 人工核对 SOP 与值班流程落地（运维文档 §5）；
5. `AGENT_EVENT_RETENTION_DAYS` 与合规留存要求对齐；
6. 租约参数（60s/15s）与环境匹配，确认 `heartbeat < lease/2`；
7. 多实例 `AGENT_RUNTIME_WORKER_ID` 唯一分配方案；
8. SECRET_KEY、TLS、网络 allowlist 复核。

## 9. 部署与回滚要点

**部署**：`pip install -r requirements.txt` → 备份业务库 → `alembic upgrade head` → 配置 `AGENT_CHECKPOINT_DSN` 等 → `uvicorn app.main:app`（Worker 随 lifespan 自动启动，多实例可横向扩容）。

**回滚**：方式 A（推荐）`AGENT_RUNTIME_ENABLED=false` 回旧引擎，不动数据；方式 B `alembic downgrade b7c8d9e0f1a2`（删新表，先确认无在途 Run 且合规允许）。

**故障处置**：F-01~18 逐场景的系统行为与运维动作见运维文档 §4；`unknown`/`needs_reconciliation` 人工核对 SOP 见 §5。

## 10. 交付物清单

| 交付物 | 路径 |
|---|---|
| 基线审查报告（R-01~R-12） | `docs/agent-runtime/phase2-baseline-review.md` |
| 架构说明 | `docs/agent-runtime/phase2-durable-execution-design.md` |
| 部署与故障恢复运维手册 | `docs/agent-runtime/phase2-deployment-and-recovery.md` |
| 测试报告（T-01~11 / F-01~18 映射） | `docs/agent-runtime/phase2-test-report.md` |
| 变更清单 | `docs/agent-runtime/phase2-change-log.md` |
| 本总结报告 | `docs/agent-runtime/phase2-completion-summary.md` |
| 代码 | `backend/app/models/agent.py`、`backend/app/services/agent_runtime/`（service/lifecycle/events/context/worker/repositories/tools）、`backend/app/api/v1/agents.py`、`backend/app/core/{config,database}.py`、`backend/app/main.py` |
| 迁移 | `backend/alembic/versions/add_durable_execution.py`、`add_event_thread_id.py`、`e5f6a7b8c9d0_add_agent_base_tables.py` |
| 测试 | `backend/tests/test_agent_runtime_durable.py`（47 项）等 4 个文件共 105 项 |

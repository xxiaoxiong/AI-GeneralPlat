# Phase 2.1 Durable Execution 加固 — 变更清单（Change Log）

> 任务来源：`docs/AI-GeneralPlat_Phase2.1_DurableExecution_Hardening_TaskBook.md`
> 覆盖批次：Batch 1 ~ Batch 8（每批独立提交）
> 说明：本文只记录 Phase 2.1 加固的增量变更；Phase 2 基线变更见 `phase2-change-log.md`。

---

## 0. 提交索引

| Batch | Commit | 主题 | 对应任务书 |
|---|---|---|---|
| B1 | `daf9704` | Checkpointer 持久化与多进程部署入口 | P0-01 / P0-06 |
| B2 | `8a09867` | Run Lease 全条件 Fencing + 数据库级状态机闭环 | P0-02 |
| B3 | `07226df` | Tool Ledger 执行权一致 + 工具结果语义统一 | P0-03 / P0-05 |
| B4 | `9e0bc66` | 恢复决策表 + 幂等契约声明 + Checkpoint 损坏安全阻断 | P0-04 |
| B5 | `f1072f6` | API 幂等收敛 + Session 执行权/消息乐观锁/Outbox 写回 | P1-01 / P1-02 |
| B6 | `51e7395` | HITL 多轮审批恢复 + 取消/崩溃恢复 + 人工核对接口 | P1-03 / P1-04 |
| B7 | `4ce0c2d` | Run 级执行预算持久化 + 事件批量持久化/订阅回放 | P1-05 / P1-06 |
| B8 | （本次提交） | 安全基线 + SEC 测试 + CI + 交付文档 | P1-07 / §15 / §17 / §18 |

---

## 1. 数据库迁移（全部独立新增，不修改历史迁移语义）

迁移链（`backend/alembic/versions/`，head = `d7e8f9a0b1c2`）：

```
9ce3d801b702 initial（历史）
  → e5f6a7b8c9d0  agent 基础表（历史）
  → a1b2c3d4e5f6  database_connections（历史）
  → b7c8d9e0f1a2  agent_runs / agent_approvals 基础表（历史）
  → d4e5f6a7b8c9  Phase 2 durable execution 主迁移（历史）
  → f1a2b3c4d5e6  agent_run_events + thread_id（历史）
  → e3f4a5b6c7d8  add_run_recovery_metadata        （Phase 2.1，B4）
  → b5c6d7e8f9a0  add_session_concurrency_outbox   （Phase 2.1，B5）
  → c6d7e8f9a0b1  add_reconcile_and_cancel         （Phase 2.1，B6）
  → d7e8f9a0b1c2  add_run_budget                   （Phase 2.1，B7，head）
```

### 1.1 各迁移的持久化模型变更

| 迁移 | 表 | 变更 |
|---|---|---|
| `add_run_recovery_metadata.py`（B4） | `agent_runs` | +`recovery_metadata`（JSON，恢复决策记录：决策类型/依据/外部 operation ID 等） |
| `add_session_concurrency_outbox.py`（B5） | `agent_sessions` | +`version`（乐观锁）、+`active_run_id`、+`active_run_lease_expires_at`（Session 执行权租约） |
| 同上 | `session_result_outbox`（新表） | Session 写回 Outbox：`run_id`/`session_id`/`status`(pending→delivered)/`attempts`/`last_error`/时间戳；索引 `ix_session_result_outbox_session_id`、`ix_session_result_outbox_status` |
| `add_reconcile_and_cancel.py`（B6） | `agent_runs` | +`cancel_requested`（bool，CAS 写入）、+`cancel_requested_at`、+`cancel_requested_by` |
| 同上 | `agent_tool_executions` | +`reconcile_decision`（confirmed_succeeded / confirmed_not_executed / unresolved）、+`reconcile_note`、+`reconciled_by`、+`reconciled_at`（人工核对审计） |
| `add_run_budget.py`（B7） | `agent_runs` | +`budget_model_calls`/`budget_tool_calls`/`budget_steps`/`budget_tokens`（跨恢复累计）、+`budget_first_started_at`、+`budget_deadline_at`、+`budget_version`（预算 CAS）、+`hitl_wait_started_at`（HITL 等待不计入超时） |

所有迁移均使用 `batch_alter_table`（SQLite 兼容）且自带完整 `downgrade`；已实测 upgrade → 断言 → downgrade → upgrade 往返（SQLite / MySQL）。

---

## 2. 各 Batch 代码变更详情

### 2.1 B1 `daf9704` — P0-01 / P0-06：Checkpointer 持久化与多进程部署入口

- `app/services/agent_runtime/persistence.py`：Checkpointer 选型收敛——生产 Durable Runtime **不再使用 Memory Checkpointer**；按 `AGENT_CHECKPOINT_DSN` 选择 `AsyncPostgresSaver` / `AsyncMySQLSaver` / SQLite 文件；多进程共享同一持久化 Checkpointer。
- `app/core/config.py` / `database.py` / `logging.py` / `main.py`：新增 `AGENT_CHECKPOINT_DSN` 等配置；SQLite WAL + 锁等待；lifespan 启停 RunWorker。
- `backend/.env.example` / `README.md`：新增配置说明。
- `docker/backend.Dockerfile` / `backend-entrypoint.sh` / `docker-compose.yml`：启动迁移入口（`alembic upgrade head` 失败即退出，不带病启动）→ `uvicorn --workers 2` 多进程共享持久化 Checkpointer。
- `requirements.txt`：补 `psycopg`、`aiomysql`/`PyMySQL`。
- 测试：`test_checkpointer_persistence.py`（多进程共享 Checkpointer）、`test_migration_mysql.py`。

### 2.2 B2 `8a09867` — P0-02：Run Lease 全条件 Fencing + 数据库级状态机闭环

- `repositories/run_repository.py`：Claim / 心跳 / 状态提交 / 账本写入全部携带 `worker_id + lease_version` 条件（CAS）；Lease 过期后旧 Worker 无法续租、提交 Run 或写 Tool Ledger；非法状态迁移与终态覆盖在**数据库层**被拒绝（`IllegalTransition` / `RunLeaseLost`）。
- `repositories/errors.py`：新增上述错误类型，服务层显式上抛不吞。
- `lifecycle.py` / `service.py` / `worker.py` / `tools/adapter.py`：执行权校验贯穿执行与副作用写入路径。
- 测试：`test_run_lease_fencing.py`。

### 2.3 B3 `07226df` — P0-03 / P0-05：Tool Ledger 执行权一致 + 工具结果语义统一

- `repositories/tool_execution_repository.py`：账本写入强制校验执行权与参数指纹（`args_hash`）一致性；工具错误不得被误记为 `succeeded`。
- `services/tools/result.py` + `services/tools/*.py`（web/database/knowledge/math/network/notify/code）：统一 `ToolResult` 语义（`ok / error / unknown` 三态与文本/结构化载荷）；`unknown` 表示外部副作用结果不确定，禁止自动重放。
- `services/agent/tool_executor.py` / `agent_service.py` / `persistence.py` / `tools/adapter.py`：结果语义贯通到账本状态机（`prepared → executing → succeeded/failed/unknown → needs_reconciliation`）。
- 测试：`test_tool_ledger_guard.py`、`test_tool_result_semantics.py`。

### 2.4 B4 `9e0bc66` — P0-04：恢复决策表 + 幂等契约声明 + Checkpoint 损坏安全阻断

- `tools/classification.py`：工具副作用分类与**恢复决策表**（RC-01~RC-06）——可安全执行 / 审批挂起 / 副作用不确定（`unknown` → `needs_reconciliation` 人工核对）/ Checkpoint 缺失或损坏（安全阻断，**不自动重新推理**）。
- `agent_runs.recovery_metadata`（迁移 `e3f4a5b6c7d8`）：持久化恢复决策与依据。
- `worker.py` / `service.py`：Checkpoint 丢失/损坏时显式失败并写 `recovery_error`，杜绝对不确定副作用盲目重放。
- 各工具登记**幂等契约声明**（是否支持下游幂等键、透传方式）。
- 测试：`test_recovery_decision.py`。

### 2.5 B5 `f1072f6` — P1-01 / P1-02：API 幂等收敛 + Session 并发控制 + Outbox 写回

- `api/v1/agents.py` / `service.py`：`Idempotency-Key` 收敛到统一入口；同键同请求摘要复用原 Run，不同摘要 409（`IdempotencyConflict`），跨 Agent/用户作用域不串。
- `repositories/session_repository.py`：Session 执行权租约 + 消息写回乐观锁（`agent_sessions.version`）；同 Session 并发 Run 按租约串行（SS-01）。
- `session_result_outbox`：写回经 Outbox 投递，幂等重试不重复 Assistant 消息（SS-02），写回失败不改变 Run 终态（SS-03）。
- 测试：`test_api_idempotency_session.py`、`test_migration_mysql.py`（扩展）。

### 2.6 B6 `51e7395` — P1-03 / P1-04：HITL 多轮恢复 + 取消/崩溃恢复 + 人工核对接口

- `service.py`：`resume_run` 只按**当前 Checkpoint Interrupt 批次**的动作顺序构造 `Command(resume=...)` 的 decisions 数组（与 `action_requests` 按位置对齐），历史批次决策不再带入；`_match_approval_row` 禁止按下标匹配，`action_id` 精确匹配；决策写入 CAS（`WHERE action_id=? AND decision IS NULL`），重复相同决策幂等、冲突 409。
- `api/v1/agents.py`：新增 `POST /runs/{run_id}/reconcile`（超管权限）——`confirmed_succeeded` / `confirmed_not_executed` / `unresolved` 三处置，含审计、外部 operation ID、条件状态迁移；Checkpoint 丢失时不自动重新推理。
- `agent_runs` +`cancel_requested*`：取消经 CAS 写入，Worker 心跳轮询感知；**在途外部工具不伪造取消**（如实报 unknown/failed）；pending 取消与 Claim 互斥。
- `worker.py`：堵洞——`run_claimed` 时 `job.resume is not None`（恢复执行）不再走 `RECOVERY_FRESH`，按恢复决策表处置。
- `tool_execution_repository.py`：`needs_reconciliation` 是死终态，仅 reconcile 接口可迁移（`reconcile_decision/note/by/at` 审计列）。
- 运维文档 `phase2-deployment-and-recovery.md` §5：手工 UPDATE 账本 SOP 改为调用 reconcile API。
- 测试：`test_agent_runtime_durable.py` / `test_api_smoke.py` 扩展（HITL-01~04、RC 场景）。

### 2.7 B7 `4ce0c2d` — P1-05 / P1-06：预算持久化 + 事件批量持久化/订阅回放

- `agent_runs` +8 预算列（见 §1.1）：模型/工具/步数/token 计数**跨恢复累计不重置**（BG-01）；`budget_deadline_at` + `hitl_wait_started_at` 支持恢复时超 deadline → `limit_exceeded`（BG-02）；`budget_version` CAS 防并发累计丢失。
- `events.py`：事件**批量持久化**（合并事务，100 token/s 输出不产生每 token 一次独立 DB 事务，EV-04）；多订阅者完整回放（EV-01）；慢消费者 Queue Overflow 由数据库补齐（EV-02）；`Last-Event-ID` / `after_seq` 断线续传不重不漏（EV-03）。
- 测试：`test_agent_runtime_durable.py` 扩展（EV/BG 场景）。

### 2.8 B8（本次）— P1-07 安全基线 + CI + 交付文档

安全改造（`tools/policy.py` 为核心）：

- **A 工具风险显式注册表**：`ToolRiskProfile(risk, side_effect_class, requires_approval, retry_policy)` 四元组显式登记（含 DeepAgents 文件工具、python_exec、http_request 别名）；**未登记工具默认拒绝执行**（fail-closed）。
- **B SSRF 统一入口**：`check_url` 统一校验（scheme、allowlist 精确/子域匹配——删除 `startswith` 前缀绕过、内网/回环/链路本地/云元数据拦截、DNS 解析 fail-closed）；`webhook_url` / `smtp_host` 参数统一走该入口（SEC-01/02）；SMTP 收信前 `getpeername()` 对端 IP 校验；`AGENT_EGRESS_PROXY` 受控出口（完整防护需部署侧 Egress Proxy，见 `phase2.1-deployment.md`）。
- **C 路径防护**：`check_path` 改 `realpath` 比较，防符号链接逃逸（SEC-03）。
- **D SQL 只读强化**：剥离注释/字符串后校验；拒绝多语句（仅允许单尾分号）；首词白名单（select/with/show/describe/explain 等）；词边界禁词扫描（insert/update/delete/drop/alter/truncate/create/grant/revoke/merge），堵 CTE 写入绕过（SEC-04）。
- **E 脱敏升级**：`redact_secrets`（不截断）/`redact_sensitive`（截断）——敏感键子串匹配、URL userinfo 与 token/secret 等查询参数、`Authorization: Bearer` 文本；覆盖事件流、工具账本、审批、工具输出与模型回传（SEC-05）。
- **F 代码执行诚实分级**：`restricted`（AST 级限制 + 线程超时，**不是安全沙箱**）/ `disabled` / `external`；生产（`is_production`）无外部沙箱时默认禁用（SEC-06）；`AGENT_ALLOW_UNSAFE_PYTHON` 开发逃生开关生产强制关闭；超时线程诚实语义（daemon 线程无法强制终止，登记并阻止后续执行）。
- 配置：`app/core/config.py` +`AGENT_ALLOW_UNSAFE_PYTHON`、`AGENT_EGRESS_PROXY`。
- CI：`.github/workflows/ci.yml`（Python 3.11 单进程 SQLite 全量 + MySQL8 Migration/Repository + PostgreSQL16 Checkpointer 关键集；单 pytest 进程不争抢共享 SQLite 库）、`e2e.yml`（真实模型 E2E 独立 Secret，未配置显式 skipped 不计入基础 CI）。
- 测试：`tests/test_agent_runtime_security.py` 30 项（SEC-01~08）。

---

## 3. API 契约变更（向后兼容，只加不改）

| 接口 | 变更 |
|---|---|
| `POST /agents/{id}/chat`、`/invoke` | 可选 `Idempotency-Key` 请求头（B5 收敛统一入口） |
| `POST /runs/{run_id}/cancel` | CAS 取消语义跨 Worker 生效（B6）；既有路径/字段不变 |
| `POST /runs/{run_id}/resume` | HITL 决策按当前批次精确匹配（B6）；请求/响应形状不变 |
| `POST /runs/{run_id}/reconcile` | **新增**（B6）：`confirmed_succeeded` / `confirmed_not_executed` / `unresolved` 三处置 + 审计 |
| SSE 事件流 | `Last-Event-ID` / `after_seq` 续传、批量持久化（B7）；旧客户端行为不变 |

## 4. 配置项变更（新增）

| 配置 | 默认 | 说明 |
|---|---|---|
| `AGENT_CHECKPOINT_DSN` | 空（SQLite 文件回退） | 持久化 Checkpointer DSN，生产建议 PostgreSQL |
| `AGENT_ALLOW_UNSAFE_PYTHON` | `false` | 开发逃生开关；`is_production` 强制为 false |
| `AGENT_EGRESS_PROXY` | 空 | HTTP(S) 工具受控出口代理 URL |

## 5. 测试适配说明（如实披露，断言未改动）

B8 引入"未登记工具默认拒绝 / URL 必须通过 Policy"等有意行为变化，以下测试仅调整 setup/夹具输入以适配新契约，**全部断言保持原样**：

1. `test_agent_runtime.py::test_tool_failure_reported_to_model`：夹具 `broken_tool` 现需显式 `register_tool_risk(...)`（try/finally 注销），否则按 fail-closed 被拒——这正是 SEC-08 的预期行为。
2. `tests/conftest.py`：追加 `AGENT_NETWORK_ALLOWLIST=example.com` 测试夹具域显式信任（服务级流程测试的 `hooks.example.com` 等夹具 URL；SEC 测试直构 Policy 不受影响）。
3. `test_tool_result_semantics.py::_SlowSMTP`：补连接 socket 桩（`getpeername()` 返回公网对端），模拟 SMTP 对端校验通过的真实连接。
4. `test_agent_runtime_durable.py` t03c：夹具 `"webhook_url": "u"` → `https://hooks.example.com/x`（URL Policy 需合法夹具地址）。

## 6. 已知边界（不夸大声明）

- **不声称 exactly-once**：外部副作用只承诺"至少一次 + 可检测重复 + 重复可控"，`unknown` 依赖人工核对最终一致。
- `restricted` 代码执行不是安全沙箱；防逃逸依赖 `external` 沙箱或生产禁用。
- SSRF 的 DNS Rebinding 完整防护需部署侧 Egress Proxy / 网络层策略，应用层仅提供等价校验与代理挂载点。
- SQLite 验证通过**不算生产库通过**；生产库结论以 MySQL 8.0 / PostgreSQL 16 实测为准（见 `phase2.1-test-report.md`）。

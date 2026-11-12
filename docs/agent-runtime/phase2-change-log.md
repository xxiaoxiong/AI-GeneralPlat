# Phase 2 Durable Execution 变更清单

> 范围：任务书 FR-01 ~ FR-10 对应的全部代码/迁移/测试/文档变更。

## 1. 新增文件

| 文件 | 内容 | 原因 |
|---|---|---|
| `backend/alembic/versions/e5f6a7b8c9d0_add_agent_base_tables.py` | 空库基线修复：补齐初始迁移缺失的 agents 等基础表 | 空库 `alembic upgrade` 链断裂，历史迁移依赖 agents 表 |
| `backend/alembic/versions/add_durable_execution.py`（`d4e5f6a7b8c9`） | Phase 2 主迁移：agent_runs 幂等/租约列、agent_approvals 动作身份列、新增 `agent_tool_executions`、`agent_run_events` | FR-01/02/04/05/07 数据模型 |
| `backend/alembic/versions/add_event_thread_id.py`（`f1a2b3c4d5e6`） | `agent_run_events` 增加 `thread_id` | FR-07 事件回放需线程维度 |
| `backend/app/services/agent_runtime/repositories/run_repository.py` | RunRepository：幂等创建/Claim/heartbeat/release/fencing 提交/孤儿扫描 | FR-01/02/03 |
| `backend/app/services/agent_runtime/repositories/tool_execution_repository.py` | ToolExecutionRepository：账本状态机（prepared→executing→succeeded/failed/unknown/needs_reconciliation） | FR-05/06 |
| `backend/app/services/agent_runtime/repositories/event_repository.py` | EventRepository：sequence 分配、追加、after_seq 查询、保留清理 | FR-07 |
| `backend/app/services/agent_runtime/repositories/errors.py`、`__init__.py` | `IdempotencyConflict` 等仓储异常与导出 | FR-01 |
| `backend/app/services/agent_runtime/worker.py` | RunWorker：扫描领取、心跳续租、优雅关闭、事件保留清理、`get_run_worker()` | FR-02/03 |
| `backend/app/services/agent_runtime/tools/classification.py` | 工具副作用分类（只读/本地/外部副作用） | FR-05 按类定重试策略 |
| `backend/tests/test_agent_runtime_durable.py` | 47 项：FR-01~10 单元/并发/恢复/故障注入 + T-01~11 + 迁移 upgrade/downgrade | 任务书 §10 测试要求 |
| `docs/agent-runtime/phase2-baseline-review.md` | Phase 0 基线审查（R-01~R-12） | 任务书"先审查后修改" |

## 2. 修改文件

| 文件 | 变更内容 | 原因 |
|---|---|---|
| `backend/app/models/agent.py` | AgentRun/AgentApproval 扩展列；新增 AgentToolExecution、AgentRunEvent 模型 | FR-01/04/05/07 |
| `backend/app/services/agent_runtime/service.py` | 执行链路持久化：幂等创建、租约提交（fencing）、账本登记、审批持久化/幂等/冲突、事件持久化、`_consume_stream` 权威收集 final_text、`_write_back_session`、`_load_prior_final_text`、BudgetExceededError→`limit_exceeded`、关键持久化异常上抛 | FR-03/04/05/06/08/09/10 |
| `backend/app/services/agent_runtime/lifecycle.py` | RunHandle 增 `final_text_parts`；状态机扩展 `limit_exceeded`/`needs_reconciliation` 转移 | FR-09/10 |
| `backend/app/services/agent_runtime/events.py` | 发布即持久化；payload 脱敏但文本型事件不截断（回放保真） | FR-07/09 |
| `backend/app/services/agent_runtime/context.py` | `stable_message_id`、`messages_to_langchain`（REMOVE_ALL_MESSAGES 确定性重播种）、`ModelUsageCallback`、RuntimeContext 计数字段 | FR-08/10 |
| `backend/app/services/agent_runtime/tools/adapter.py` | 执行前后账本登记/确认、`raise_on_error` 区分 failed/unknown、预算超限抛 `BudgetExceededError` | FR-05/10 |
| `backend/app/services/agent_runtime/tools/policy.py` | 新增 `BudgetExceededError`（映射 `limit_exceeded` 不误报 failed） | FR-10.7 |
| `backend/app/api/v1/agents.py` | `Idempotency-Key` 支持；FR-08.1 用户消息 Run 创建前预存（失败 500 不启动运行）；运行态查询走数据库；Runtime 路径会话写回移交服务层 | FR-01/08 |
| `backend/app/core/config.py` | 新增 `AGENT_CHECKPOINT_DSN`、`AGENT_STORE_DSN`、`AGENT_RUNTIME_*`（worker/lease/heartbeat/poll/shutdown）、`AGENT_EVENT_RETENTION_DAYS/CLEANUP_INTERVAL` 等 | FR-02/07 运维配置 |
| `backend/app/core/database.py` | SQLite 启用 WAL + 30s 锁等待 | 并发写入（Worker/事件）不互锁 |
| `backend/app/main.py` | lifespan 挂载 RunWorker 启动/优雅停止 | FR-02/03 |
| `backend/app/api/v1/workflows.py` | 执行前提交事务释放写锁 | 避免长事务与运行时持久化互相阻塞 |
| `backend/app/services/tools/__init__.py`、`network_tools.py` | `ToolExecutor.execute(raise_on_error=)` 参数 | 账本需真实异常区分 failed/unknown |
| `backend/tests/test_agent_runtime.py`、`test_api_smoke.py` | 回归适配 + SSE 回放/预算状态用例 + Idempotency-Key API 用例 2 项（`test_invoke_idempotency_key_hits_original_run`、`test_chat_idempotency_key_no_duplicate_execution`） | 验证 API 兼容 |
| `backend/alembic/versions/add_database_connections.py` | `down_revision` 重接至 `e5f6a7b8c9d0`（线性化升级链）；ALTER 改 `batch_alter_table`（SQLite 兼容），**语义不变** | 空库 `upgrade head` 原本无法通过（双头分叉 + SQLite 不支持 ADD CONSTRAINT）。历史迁移文件唯一的例外修改，如实披露 |

## 3. 数据库变更汇总

- **修改表**：
  - `agent_runs` +11 列（`request_hash`、`input_payload`、`agent_config_snapshot`、`started_at`、`worker_id`、
    `lease_expires_at`、`heartbeat_at`、`attempt`、`lease_version`、`last_event_seq`、`recovery_error`）；
  - `agent_approvals` +6 列（`action_id`、`tool_call_id`、`args_hash`、`action_payload`、`decision_status`、`updated_at`）；
- **新增表**：`agent_tool_executions`、`agent_run_events`；
- **新增唯一约束**：`uq_agent_runs_idem(user_id, idempotency_key)`、`uq_agent_approvals_action(run_id, action_id)`、
  `uq_tool_exec_run_call(run_id, tool_call_id)`、`uq_run_event_seq(run_id, sequence)`；
- **新增索引**：`ix_tool_exec_run_status`、`ix_tool_exec_status_updated`、`ix_run_event_run_seq` 等；
- **Migration revision**：`d4e5f6a7b8c9` + `f1a2b3c4d5e6`（head）+ 基线修复 `e5f6a7b8c9d0`；
- **Upgrade / Downgrade 验证**：SQLite 实跑 `upgrade head → downgrade base → upgrade head` 通过
  （含表/列/唯一约束断言）；MySQL/PostgreSQL 未实测（见测试报告 §8）。

## 4. API 兼容性

- `POST /agents/{id}/chat`、`GET /runs/{id}`、`POST /runs/{id}/cancel`、`POST /runs/{id}/resume`
  路径与既有请求/响应字段**保持不变，仅新增可选字段/请求头**（`Idempotency-Key`；run 响应新增
  租约/attempt/预算摘要等字段）；
- `AGENT_RUNTIME_ENABLED=false` 为整体回退开关（旧引擎路径）；
- SSE 新增 `Last-Event-ID` / `after_seq` 续传能力，旧客户端不带参数时行为不变。

## 5. 文档

| 文件 | 内容 |
|---|---|
| `docs/agent-runtime/phase2-durable-execution-design.md` | 架构说明（状态源职责、数据模型、FR-01~10 机制） |
| `docs/agent-runtime/phase2-deployment-and-recovery.md` | 部署/回滚/故障处置 F-01~18/人工核对 SOP |
| `docs/agent-runtime/phase2-test-report.md` | 测试报告（T-01~11、故障注入映射、限制） |
| `docs/agent-runtime/phase2-change-log.md` | 本文件 |

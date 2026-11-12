# Phase 2 基线审查（baseline review）

> 任务书：`docs/AI-GeneralPlat_AgentRuntime_Phase2_DurableExecution_TaskBook.md`
> 审查时间：2026-11-12 ｜ 审查人：Trae
> 结论先行：**工作区 HEAD = `36fe3fb`，与任务书声明的审阅基线 commit 完全一致**；
> R-01~R-12 风险逐条核实完毕，除 R-04 部分缓解外**全部仍然存在**，本阶段按任务书逐项修复。

---

## 1. 审查环境

### 1.1 Git 状态

| 项 | 值 |
|---|---|
| 分支 | `main`（与 `origin/main` 同步） |
| HEAD | `36fe3fb 第一阶段改造` = 任务书基线 commit |
| 未提交改动 | 无（仅 untracked：本阶段任务书文档） |

### 1.2 依赖版本（实测）

| 包 | 版本 | 备注 |
|---|---|---|
| python | 3.11 | |
| fastapi | 0.162.0 | |
| sqlalchemy | 2.1.8 | async ORM |
| pydantic | 2.14.3 | |
| langchain | 1.4.9 | |
| langchain-core | 1.7.6 | 含 `InjectedToolCallId`（FR-05 工具调用身份注入，见 §5） |
| langgraph | 1.2.20 | |
| deepagents | 0.7.34 | |
| langgraph-checkpoint | 4.3.1 | |
| langgraph-checkpoint-sqlite | 3.1.3 | |
| langgraph-checkpoint-postgres | **未安装** | psycopg **未安装** → PostgreSQL Checkpointer 当前不可用 |

### 1.3 数据库现状

| 项 | 现状 | 影响 |
|---|---|---|
| 生产配置 | `DATABASE_URL=mysql+aiomysql://...@localhost:3307/ai_plat`（[config.py](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/core/config.py#L14)） | 项目业务库是 **MySQL**，不是任务书假设的 PostgreSQL |
| MySQL 服务 | 3306/3307 端口均未监听（`TcpTestSucceeded=False`） | 与阶段一相同的阻塞；Alembic 迁移无法对 MySQL 实测 |
| 测试库 | 临时 SQLite（`sqlite+aiosqlite`，[conftest.py](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/tests/conftest.py#L11)） | 迁移与全部自动化测试以 SQLite 验证 |
| Alembic | 3 个 revision：`9ce3d801b702_initial` → `a1b2c3d4e5f6`（add_database_connections）→ `b7c8d9e0f1a2`（[add_agent_runs](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/alembic/versions/add_agent_runs.py)） | 新迁移以 `b7c8d9e0f1a2` 为 down_revision |

### 1.4 关键设计决策（由数据库现实决定）

1. **Claim/Lease 用"原子条件 UPDATE（CAS）"实现，不用 `SELECT ... FOR UPDATE SKIP LOCKED`。**
   理由：业务库是 MySQL 8+（支持 SKIP LOCKED）但测试库是 SQLite（不支持行锁语义）；单条
   `UPDATE ... WHERE 资格条件 ...` 在三种数据库上都是原子操作，满足任务书 FR-02"Claim 必须是原子操作，
   不能先 SELECT 再无锁 UPDATE"的硬性要求，且不引入方言分叉。fencing 用 `lease_version` 条件递增。
2. **PostgreSQL Checkpointer 保持代码分支但不在本阶段安装 psycopg**；生产持久化 Checkpointer
   用 `langgraph-checkpoint-sqlite`（文件库）或后续按部署文档切 Postgres。
   任务书 §13.2"memory 应启动失败或强告警"照做（durable 模式下拒绝 `memory`）。
3. **Alembic 迁移在 SQLite 上完整 upgrade/downgrade 验证**，MySQL 实测步骤写入部署文档并标注
   "因本机 MySQL 未运行暂未实测"，不伪造结果。

---

## 2. 风险核实表（任务书 §1.3，逐条对照当前代码）

| 编号 | 风险 | 核实结论 | 证据 |
|---|---|---|---|
| R-01 | RunRegistry 进程内字典 / RunHandle.task 进程内 asyncio.Task | **仍存在** | [lifecycle.py](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/services/agent_runtime/lifecycle.py) `RunRegistry._by_id`/_by_idem 内存 dict |
| R-02 | `resume_run()` 依赖 `registry.get(run_id)` | **仍存在** | service.py `resume_run` 仅从内存 registry 取 handle |
| R-03 | `AGENT_CHECKPOINT_DSN` 默认 `memory` | **仍存在** | [config.py#L98](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/core/config.py#L98) |
| R-04 | `idempotency_key` 仅普通索引，无唯一约束 | **仍存在**（模型 [agent.py#L70](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/models/agent.py#L70) 与迁移 b7c8d9e0f1a2 均为普通 index） |
| R-05 | AgentApproval 缺稳定 action/tool-call 身份 | **仍存在** | [agent.py#L80-93](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/models/agent.py#L80-L93) 仅 tool_name/decision |
| R-06 | 工具执行无持久化账本 | **仍存在** | tools/adapter.py 直接执行，无 ledger |
| R-07 | EventBus 内存 Queue 满丢最旧事件 | **仍存在** | events.py `EventBus.publish` 丢最旧 |
| R-08 | SSE 无持久化事件源 / 无 after_seq 回放 | **仍存在** | 无 `agent_run_events` 表；`GET /runs/{run_id}/events` 不存在 |
| R-09 | 每轮全量 Session 消息传入已有 Thread | **仍存在** | [agents.py#L324-325](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/api/v1/agents.py#L324-L325) 全量 messages 进 Graph |
| R-10 | final_text 由 LegacyEventAdapter 累积，存在竞态 | **仍存在** | [service.py#L150,388](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/services/agent_runtime/service.py#L150) 取 `adapter.final_text_parts`；[events.py#L174](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/services/agent_runtime/events.py#L174) 在 adapt 中 append |
| R-11 | `ctx.model_calls` 按非空 chunk 文本递增 | **仍存在** | [service.py#L442-444](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/services/agent_runtime/service.py#L442-L444) |
| R-12 | `_persist_run()` 捕获异常只记日志 | **仍存在** | [service.py#L594-595](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/services/agent_runtime/service.py#L594-L595) |

## 3. 与任务书的其他差异 / 需注意的现状事实

| 项 | 现状 | 处理 |
|---|---|---|
| 状态机 | `ALLOWED_TRANSITIONS` 无 `needs_reconciliation` | Phase 2 扩展状态机（终态语义按 FR-03 表） |
| SSE 响应 | `X-Run-Id` 头已存在；事件 JSON 为旧契约 `type/content/tool/input`，`seq` 已在外层 | 回放接口复用同一契约，新增 `id:`（SSE event id）不破坏前端 |
| 前端消费 | [AgentChat.vue](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/frontend/src/views/agents/AgentChat.vue) `fetch` + `getReader()` 解析 `data:` JSON，识别 thought/action/observation/final/final_chunk/done/error | 新增字段一律可选；不改前端 |
| HITL | `resume_run` 内存恢复 + `AgentApproval` 手工落库（无 action_id） | Phase 4 重构 |
| 模型调用计数 | 按 chunk 计 | Phase 6 改为模型请求级计数（LangChain callback / START 事件） |
| 旧引擎回滚开关 | `AGENT_RUNTIME_ENABLED` 仍保留 | 不动 |
| 工具调用身份 | langchain-core 1.7.6 支持 `InjectedToolCallId` | FR-05 直接采用，禁止用 args_hash 兼当调用 ID |

## 4. 最终数据模型草案（目标语义，字段名按现有模型风格）

### 4.1 `agent_runs` 新增列

| 列 | 类型 | 用途 |
|---|---|---|
| `request_hash` | String(64) | 规范化请求摘要（幂等冲突判定） |
| `input_payload` | JSON | 恢复所需输入快照（消息引用 + 参数，无密钥） |
| `agent_config_snapshot` | JSON | 无敏感明文的执行配置快照（脱敏后） |
| `started_at` | DateTime | 首次开始执行时间 |
| `worker_id` | String(128) | 当前租约持有者 |
| `lease_expires_at` | DateTime | 租约到期 |
| `heartbeat_at` | DateTime | 最近心跳 |
| `attempt` | Integer default 1 | 执行/恢复尝试次数 |
| `lease_version` | Integer default 0 | fencing token |
| `last_event_seq` | BigInteger default 0 | 最近事件序号（冗余加速） |
| `recovery_error` | Text | 恢复失败原因 |

唯一约束：`uq_agent_runs_idem` on `(user_id, idempotency_key)`（MySQL/SQLite 均允许多 NULL，未传幂等键保持旧行为）。
状态集扩展：+ `needs_reconciliation`。

### 4.2 `agent_approvals` 新增列

`action_id` String(64)（稳定动作 ID）、`tool_call_id` String(64)、`args_hash` String(64)、
`action_payload` JSON（脱敏恢复信息，可空）、`decision_status` String(24)（pending/approved/rejected/superseded）、
`updated_at`。唯一约束 `uq_agent_approvals_action` on `(run_id, action_id)`。

### 4.3 新表 `agent_tool_executions`

见任务书 §5.3 全字段；`UNIQUE(run_id, tool_call_id)`、`INDEX(run_id, status)`、`INDEX(status, updated_at)`。
状态机：`prepared → executing → succeeded | failed | unknown → needs_reconciliation`。

### 4.4 新表 `agent_run_events`

`id` / `run_id` / `sequence` / `event_type` / `payload`(JSON, 已脱敏) / `created_at`；
`UNIQUE(run_id, sequence)`、`INDEX(run_id, sequence)`。
序号并发安全方案：每次插入前 `UPDATE agent_runs SET last_event_seq = last_event_seq + 1 WHERE id=:run_id`
原子自增并读回（不依赖 Worker 本地计数器），同一事务内插入事件行。

## 5. 关键流程时序（现状 → 目标差异点）

```text
现状（Phase 1）：API → start_run → registry.create → asyncio.create_task(_drive) → 内存 bus → SSE
目标（Phase 2）：API → 事务(pending + 事件) → Worker Claim(CAS+lease) → _drive(原 thread_id)
                → Tool Ledger 先登记再执行 → 事件先落库再通知 → SSE 按 seq 回放
```

详细时序图随 `phase2-durable-execution-design.md` 交付。

## 6. 风险与兼容性清单

1. **API 兼容**：`POST /agents/{agent_id}/chat`、`GET /runs/{run_id}`、cancel/resume/approve 路径与旧 SSE
   字段保持；新增 `GET /runs/{run_id}/events`（after_seq / Last-Event-ID）与 `Idempotency-Key` 头为增量能力。
2. **MySQL 未运行**：迁移仅 SQLite 实测；部署文档给出 MySQL 验证命令，报告中如实标注。
3. **langgraph-checkpoint-postgres 未安装**：本阶段以 sqlite 文件 Checkpointer 作为持久化方案，
   Postgres 分支保留代码路径与文档。
4. **`resume_run` 现返回 SSE 流**：Phase 4 保持该响应形态（内部改为 DB 加载 + Worker/就地恢复）。
5. **旧引擎 `AgentEngine` 路径不动**（`AGENT_RUNTIME_ENABLED=False` 回滚开关照旧可用）。

## 7. 本阶段测试命令（基线快照）

```bash
pytest backend/tests/test_agent_runtime.py -q          # 36 项
pytest backend/tests/test_agent_runtime_persistence.py -q  # 8 项
pytest backend/tests/test_api_smoke.py -q              # 10 项
pytest backend/tests/test_agent_runtime_e2e.py -q      # 3 项（env gate，无密钥自动跳过）
```

Phase 0 完成条件已满足：设计与当前代码一致、无凭空假设；数据模型草案见 §4；风险核实见 §2。

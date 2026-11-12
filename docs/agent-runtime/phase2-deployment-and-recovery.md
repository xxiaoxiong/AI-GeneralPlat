# Phase 2 Durable Execution 运维说明：部署与故障恢复

> 面向部署/运维人员。覆盖升级部署、回滚、故障处置（F-01 ~ F-18）、人工核对 SOP 与数据保留。

---

## 1. 环境变量 / 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `AGENT_RUNTIME_ENABLED` | `true` | **总开关/回滚开关**：`false` 走旧引擎路径 |
| `AGENT_CHECKPOINT_DSN` | `memory` | `memory` / `sqlite:///./data/agent_checkpoints.db` / `postgresql+asyncpg://...`。**生产必须用 sqlite/postgres**，`memory` 仅限单机开发 |
| `AGENT_STORE_DSN` | 空 | 跨线程记忆 Store（空=不启用；postgres 可用） |
| `AGENT_RUNTIME_WORKER_ID` | 空（自动生成） | Worker 标识，建议多实例时显式设置（`hostname-pid` 或实例名） |
| `AGENT_RUNTIME_LEASE_SECONDS` | `60` | Run 租约时长；过期后可被其他 Worker 接管 |
| `AGENT_RUNTIME_HEARTBEAT_INTERVAL` | `15` | 续租间隔，**必须 < 租约的 1/2** |
| `AGENT_RUNTIME_POLL_INTERVAL` | `1.0` | 扫描 pending / 孤儿 Run 的间隔 |
| `AGENT_RUNTIME_SHUTDOWN_GRACE` | `30` | 优雅关闭时等待在途 Run 的秒数 |
| `AGENT_MAX_STEPS` / `AGENT_MAX_TOOL_CALLS` / `AGENT_MAX_MODEL_CALLS` | `20/30/30` | 执行步数与预算；超限 → `limit_exceeded` |
| `AGENT_RUN_TIMEOUT` | `300` | 单次运行墙钟超时 |
| `AGENT_ENABLE_HITL` | `true` | 高危工具人工审批 |
| `AGENT_EVENT_RETENTION_DAYS` | `30` | 运行事件保留天数（仅清理终态超期 Run） |
| `AGENT_EVENT_CLEANUP_INTERVAL` | `3600` | 保留清理执行间隔（秒） |
| `DATABASE_URL` | — | 平台业务库（Run/账本/事件/审批均在此库） |

## 2. 部署步骤（从上一版本升级）

```bash
# 1. 拉取代码、安装依赖（langgraph >= 1.2, langgraph-checkpoint >= 4.3）
pip install -r requirements.txt

# 2. 备份业务库
#    MySQL: mysqldump / 物理备份；SQLite: 停写后拷贝文件；PG: pg_dump

# 3. 执行迁移（head = f1a2b3c4d5e6）
cd backend
alembic upgrade head

# 4. 配置环境变量（至少确认 AGENT_CHECKPOINT_DSN 指向持久化存储）

# 5. 启动服务（Worker 随应用 lifespan 自动启动）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动顺序与多实例注意：

- Worker 随 FastAPI lifespan 启动/停止（`app/main.py`），无需独立进程；
- **多实例可直接横向扩容**：Claim/Lease 保证同一 Run 只有一个有效执行者（F-12/F-13）；
  每实例设置唯一 `AGENT_RUNTIME_WORKER_ID`；
- 跨实例事件通知是进程内的，跨实例实时性依赖客户端断线回放（`after_seq`）/状态轮询；
  同实例 SSE 体验最佳；
- SQLite 部署（单机）：已启用 WAL 与 30s 锁等待；**多实例禁止共享 SQLite 文件**，请用 MySQL/PostgreSQL。

### 2.1 迁移验证结果（本仓库实测）

- `test_alembic_migration_upgrade_downgrade`：在 SQLite 上实跑 `alembic upgrade head`
  → 断言 `agent_tool_executions`/`agent_run_events` 表、`agent_runs`/`agent_approvals` 新列、
  三组唯一约束均存在 → `alembic downgrade base` → 再 `alembic upgrade head`（回滚后可再次升级）；
- **MySQL / PostgreSQL 上的迁移未实测**（本地无实例）：迁移统一使用 `batch_alter_table`
  （MySQL 上等价普通 ALTER），但**上线前必须在同版本数据库预演 upgrade/downgrade**。

## 3. 回滚步骤

**方式 A（推荐，代码级回滚，不动数据）**：

```bash
# 设置环境变量后重启服务
AGENT_RUNTIME_ENABLED=false   # 回旧引擎路径；表结构向后兼容，无需迁移
```

**方式 B（版本级回滚）**：

```bash
# 先停服（或确认无在途 Run），再回滚代码与迁移
alembic downgrade b7c8d9e0f1a2   # 回到 durable execution 迁移之前
```

注意：

- `downgrade` 会**删除** `agent_tool_executions`、`agent_run_events` 两张表并移除新增列，
  请先确认不再需要账本/事件数据（合规审计要求下禁止降级）；
- 回滚前运行 `GET /runs?status=running|waiting_approval` 确认无在途 Run；有则先 `POST /runs/{id}/cancel`。

## 4. 故障处置手册（F-01 ~ F-18）

| 编号 | 故障窗口 | 系统行为（自动） | 运维动作 |
|---|---|---|---|
| F-01 | Run 创建事务提交前崩溃 | 无半成品；客户端带 `Idempotency-Key` 重试安全 | 无需处理 |
| F-02 | 创建后、Claim 前崩溃 | pending Run 被任意 Worker `claim_next` 发现执行 | 无需处理 |
| F-03 | Claim 后、图启动前崩溃 | 租约过期后被重新 Claim（attempt+1，fencing 递增） | 无需处理 |
| F-04 | 模型调用期间崩溃 | 依 Checkpoint 恢复；允许重发模型请求，不伪造已完成节点 | 无需处理 |
| F-05 | 只读工具执行期间崩溃 | 按工具策略安全重试 | 无需处理 |
| F-06 | 外部 POST 未发送前崩溃 | 账本 `prepared`/`executing` 允许安全执行 | 无需处理 |
| F-07 | POST 已成功、响应未收到时崩溃 | 有下游幂等键：复用同一键查询/重试；无幂等能力：标 `unknown`，**不自动重放** | 查 `agent_tool_executions.status='unknown'` 行，按 §5 核对 |
| F-08 | POST 成功、账本结果提交前崩溃 | 同 F-07，不得直接假定失败 | 同上 |
| F-09 | 账本成功、Checkpoint 未写入时崩溃 | 恢复时从账本返回已存结果，不重复执行副作用 | 无需处理 |
| F-10 | HITL 中断后进程崩溃 | Run 保持 `waiting_approval`，审批信息从数据库读取 | 无需处理，等待用户决策 |
| F-11 | 用户重复提交同一审批 | 幂等返回，不重复恢复/执行 | 无需处理 |
| F-12 | 两 Worker 同时 Claim 同一 Run | 只有一个获得有效 Lease | 无需处理 |
| F-13 | Lease 过期但旧 Worker 复活 | 旧 fencing token 失效，不能提交状态或副作用 | 观察日志中的 fencing 拒绝告警 |
| F-14 | SSE 客户端断线 | `Last-Event-ID`/`after_seq` 从持久化事件续传 | 无需处理 |
| F-15 | EventBus 内存队列满 | 持久化事件不丢，回放/轮询补齐 | 无需处理 |
| F-16 | Checkpoint 不存在或损坏 | Run 明确失败/人工恢复（`recovery_error`），**不从头重放有副作用任务** | 查 `recovery_error`，按 §5 决定重跑或作废 |
| F-17 | 数据库持久化失败 | 关键状态写入失败**立即抛出并停止后续副作用**（会话未保存则不启动运行） | 查数据库可用性/磁盘/锁等待；修复后客户端幂等重试 |
| F-18 | Session 与 Checkpoint 同含旧消息 | 稳定 ID + 整通道重播种去重，不重复追加 | 无需处理 |

## 5. 人工核对 SOP（`unknown` / `needs_reconciliation`）

1. 发现：`GET /runs/{run_id}` 状态为 `needs_reconciliation`，或巡检 SQL：

   ```sql
   SELECT run_id, tool_call_id, tool_name, args_snapshot, idempotency_key,
          external_operation_id, error, updated_at
   FROM agent_tool_executions
   WHERE status IN ('unknown', 'needs_reconciliation');
   ```

2. 核对：凭 `idempotency_key` / `external_operation_id` / `args_snapshot` 到下游系统查询该操作
   是否已生效（**必须以查代猜**）；
3. 处置：
   - 下游已生效 → 将账本行人工置 `succeeded`（补录 `result_payload`），Run 可恢复继续；
   - 下游未生效 → 将账本行置 `failed` 后触发重试（`POST /runs/{run_id}/resume`）；
   - 无法查证 → 保持 `needs_reconciliation`，业务侧人工补偿，**禁止盲目重放**；
4. 记录：所有人工处置留工单，关联 `run_id + tool_call_id`。

## 6. 监控与巡检建议

- **积压**：`agent_runs` 中 `status='pending'` 持续增长 → Worker 未运行/租约配置异常；
- **孤儿**：`status='running' AND lease_expires_at < now()` 反复出现 → 频繁崩溃或租约过短；
- **fencing 拒绝**：日志关键字（fencing / lease_version）频繁出现 → 网络分区或租约过长；
- **不确定副作用**：§5 SQL 非空 → 立即人工核对；
- **预算触顶**：`status='limit_exceeded'` 占比突增 → 检查 `AGENT_MAX_*` 配置与提示词。

## 7. 数据保留与清理

- 事件保留：`AGENT_EVENT_RETENTION_DAYS`（默认 30 天），Worker 按 `AGENT_EVENT_CLEANUP_INTERVAL`
  周期清理**终态且超期** Run 的 `agent_run_events`；`pending/running/waiting_approval/
  needs_reconciliation` 永不自动清理；
- 账本（`agent_tool_executions`）**不做自动清理**（审计依据）；归档策略由 DBA 按合规要求另行制定；
- Checkpoint：`CheckpointManager.cleanup_old_checkpoints(max_age_hours=24)` 启动时清理旧检查点
  （既有机制，保留）；
- 工具文件工作区 `AGENT_FILES_ROOT` 需要定期清理（既有运维事项）。

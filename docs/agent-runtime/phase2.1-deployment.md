# Phase 2.1 部署与运维要点（Deployment）

> 任务来源：`docs/AI-GeneralPlat_Phase2.1_DurableExecution_Hardening_TaskBook.md` §18
> 配套文档：故障场景处置与人工核对 SOP 见 `phase2-deployment-and-recovery.md`；变更明细见 `phase2.1-change-log.md`；实测数据见 `phase2.1-test-report.md`。

---

## 1. 存储要求

| 存储 | 角色 | 要求 |
|---|---|---|
| **MySQL 8.0+** | 业务库（Runs / Approvals / Tool Ledger / Events / Outbox） | 已在 8.0.46 实测迁移与关键集；字符集 utf8mb4；需支持 JSON 列；迁移使用 `batch_alter_table`，SQLite 兼容但**以生产同版本库预演为准** |
| **PostgreSQL 16+** | Checkpointer（Graph State / Interrupt / Resume） | 生产必须使用持久化 Checkpointer（`AsyncPostgresSaver`），已在 16.15 实测多进程共享与故障接管；需持久卷/托管实例，**Checkpointer 丢失 = 任务无法自动恢复** |
| SQLite | 仅开发/测试 | WAL 模式；**不算生产库通过** |

## 2. 关键配置（`backend/.env.example` 为模板）

| 环境变量 | 生产建议 | 说明 |
|---|---|---|
| `AGENT_CHECKPOINT_DSN` | `postgresql://user:pass@host:5432/db` | 持久化 Checkpointer。支持 `memory`/`sqlite:///path`/`postgresql://`；**生产严禁 memory**（启动校验直接失败，不降级）；DSN 密码自动脱敏进日志 |
| `AGENT_STORE_DSN` | 可选 | 跨线程长期记忆 Store，空则不启用 |
| `DATABASE_URL` | MySQL 业务库 DSN | 启动入口先 `alembic upgrade head`，失败即退出（不带病启动） |
| `AGENT_RUNTIME_ENABLED` | `true` | `false` 一键回退旧引擎（不动数据，首选回滚方式） |
| `AGENT_RUNTIME_WORKER_ID` | 全局唯一 | 多实例必须唯一分配（如 hostname+pid 派生） |
| `AGENT_NETWORK_ALLOWLIST` | 按需收紧 | 域名 allowlist（精确或子域匹配）；未命中 + 内网/回环/云元数据一律拦截 |
| `AGENT_SANDBOX_MODE` | `disabled` 或 `external` | `restricted` 不是安全沙箱，仅防误用 |
| `AGENT_ALLOW_UNSAFE_PYTHON` | 保持 `false` | 逃生开关；`is_production` 下强制关闭，任何配置无效 |
| `AGENT_EGRESS_PROXY` | 建议配置 | HTTP(S) 工具受控出口代理（见 §5） |
| `AGENT_EVENT_RETENTION_DAYS` | 与合规对齐 | 事件保留策略（仅清理终态且超期 Run） |
| 租约参数（60s/15s） | 按网络环境调整 | 必须保持 `heartbeat < lease/2` |

## 3. 启动与 Worker 滚动

1. `pip install -r backend/requirements.txt`（版本已锁定）。
2. **备份业务库与 Checkpointer 库**。
3. `alembic upgrade head`（Docker 入口 `backend-entrypoint.sh` 自动执行，失败退出码非 0）。
4. `uvicorn app.main:app --workers N`：RunWorker 随 FastAPI lifespan 启动/优雅停止——grace 期内等在途 Run 收尾，随后释放租约可被其他实例接管。

**滚动升级**：新旧实例可共存——租约 CAS + fencing token 保证旧 Worker 在租约过期后无法续租、提交 Run 或写 Tool Ledger；升级顺序无强约束，但建议逐实例替换并观察 `recovery_error`/`needs_reconciliation` 计数。多进程（`--workers 2+`）共享同一持久化 Checkpointer 与业务库，已在 CP-01/02 双真实进程实测。

## 4. Cloud Run / Serverless 限制（重要）

- RunWorker 是**常驻进程内守护线程**（扫描 + 心跳），Cloud Run 等 scale-to-zero 平台**不适用**作为执行节点：实例被回收 = 租约到期 → Run 变孤儿，虽可被其他实例接管恢复，但会造成频繁中断恢复开销。
- 若必须部署于此类平台：将执行节点放在长驻容器/VM，Cloud Run 仅做 API 层；或接受"每次冷启动即触发孤儿恢复"的代价。
- SSE 长连接同样受平台请求超时限制；断线靠 `Last-Event-ID`/`after_seq` 回放补齐。

## 5. Egress Proxy 与 SSRF 防护边界

- 应用层已实现：URL Policy（scheme/allowlist/内网/回环/云元数据拦截 + DNS fail-closed）、SMTP 对端 IP 校验、`webhook_url`/`smtp_host` 统一入口、路径 realpath 防符号链接逃逸、SQL 只读强化、全链路脱敏。
- **DNS Rebinding 完整防护需网络层配合**：生产建议所有出网流量经 `AGENT_EGRESS_PROXY` 指定的受控出口（Egress Proxy），由代理侧做目的地址二次校验与 IP 白名单；容器网络策略禁止 Pod 直连内网段与云元数据地址（169.254.169.254）。

## 6. 备份与回滚

| 动作 | 方式 |
|---|---|
| 备份 | 业务库（MySQL：mysqldump/物理备份）+ Checkpointer 库（PG：pg_dump/物理备份）**必须一起备份**，二者共同构成执行事实 |
| 回滚 A（首选） | `AGENT_RUNTIME_ENABLED=false` 回旧引擎，数据不动 |
| 回滚 B | `alembic downgrade`（各迁移自带可逆 downgrade；默认发布流程不执行，先确认无在途 Run 且合规允许） |
| 应用回退 | 直接回退旧镜像 + 对应迁移版本；租约/状态机为数据库级条件更新，新旧版本可安全共存 |

## 7. 故障演练与人工核对

- F-01~F-18 故障场景系统行为与运维动作、`unknown`/`needs_reconciliation` 人工核对 SOP：见 `phase2-deployment-and-recovery.md` §4/§5。
- **人工核对一律调用 `POST /runs/{run_id}/reconcile`**（`confirmed_succeeded` / `confirmed_not_executed` / `unresolved`，含审计与外部 operation ID），禁止手工 UPDATE 账本。
- 不声称 exactly-once：外部副作用只承诺"至少一次 + 可检测重复 + 重复可控"。

## 8. 上线前检查清单

1. 生产同版本 MySQL/PG 预演 `alembic upgrade head` / `downgrade`；
2. `AGENT_CHECKPOINT_DSN` 指向持久化 PG 并确认备份策略；
3. `AGENT_NETWORK_ALLOWLIST` / Egress Proxy / 网络策略复核（内网与云元数据已封堵）；
4. `AGENT_SANDBOX_MODE`、`AGENT_ALLOW_UNSAFE_PYTHON`、SECRET_KEY、TLS 复核；
5. `AGENT_RUNTIME_WORKER_ID` 唯一分配；`heartbeat < lease/2`；
6. `AGENT_EVENT_RETENTION_DAYS` 与合规留存对齐；
7. reconcile 值班流程与权限（superuser）已落地；
8. CI 三库流水线（`ci.yml`）作为合入门禁，真实模型 E2E 走 `e2e.yml` 独立 Secret。

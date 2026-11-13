# Phase 2.1 Durable Execution 加固 — 测试报告

> 任务来源：`docs/AI-GeneralPlat_Phase2.1_DurableExecution_Hardening_TaskBook.md` §16/§17/§18
> 报告日期：2026-11-13
> 原则：不改断言、不跳过、不蒙混；未实测项如实标注。**不声称 exactly-once；SQLite 通过不算生产库通过。**

---

## 1. 环境与版本

| 项 | 值 |
|---|---|
| Git Commit（验证基线） | `4ce0c2d`（B1–B7）+ Batch 8 工作区改动（安全基线/CI/文档，随 B8 提交） |
| Python | 3.11.9 |
| langgraph | 1.2.20 |
| langchain | 1.4.9 |
| langchain-core | 1.7.6 |
| deepagents | 0.7.34 |
| fastapi / uvicorn | 0.162.0 / 0.54.0 |
| sqlalchemy / alembic | 2.1.8 / 1.20.1 |
| pytest / pytest-asyncio / httpx | 9.2.0 / 1.4.1.post1 / 0.28.1 |
| MySQL（业务库） | 8.0.46（Docker `mysql:8.0`，宿主 3308，驱动 aiomysql 0.3.2 / PyMySQL 1.2.4） |
| PostgreSQL（Checkpointer） | 16.15（Docker `postgres:16-alpine`，宿主 5433，驱动 psycopg 3.2.3） |
| Checkpointer 类型 | 回归默认 **AsyncSqliteSaver**（SQLite 文件）；关键集在真实 PG 16 上验证 **AsyncPostgresSaver**（CP-01/02/04 多进程用例）；`memory` 仅存在于被拒绝的误配测试中（生产校验拒绝 Memory Checkpointer） |
| Worker / 进程数 | 测试内 RunWorker 每用例 1 个（部分用例模拟双 Worker 竞争）；CP-01/02 用 **2 个真实子进程**共享 PG Checkpointer；Docker 部署形态 `uvicorn --workers 2`（见部署文档） |

## 2. 测试分类与真实结果（§17 七分类）

### 2.1 总体结果

| 轮次 | 命令范围 | 结果 | 日志 |
|---|---|---|---|
| SEC 安全基线 | `tests/test_agent_runtime_security.py` | **30 passed** | `log/b8-t1.txt` |
| SQLite 全量回归 | `pytest tests/`（单进程全部 16 个测试文件） | **301 passed / 5 skipped / 0 failed** | `log/b8-t4.txt` |
| PG 16 关键集 | `-k 'p15 or p16 or t01 or t03 or t09 or t12'`（25 项，含 CP 多进程用例） | **25 passed** | `log/b8-t5-pg.txt` |
| MySQL 8 关键集 | 同上 25 项（业务库 DSN=3308） | **25 passed** | `log/b8-t6-mysql.txt` |
| MySQL 迁移实测 | `tests/test_migration_mysql.py`（DSN=3308） | **2 passed**（upgrade→断言→downgrade→upgrade） | `log/b8-t8-mig.txt` |

### 2.2 5 个 skip 明细（如实标注，不计蒙混）

| skip | 原因 | 性质 |
|---|---|---|
| 3× 真实模型 E2E（`test_agent_runtime_e2e.py`） | 未配置 `AGENT_E2E_BASE_URL/API_KEY/MODEL` Secret，测试自身显式 skip | 真实服务缺失，**未验证**；CI `e2e.yml` 提供独立 Secret 通道 |
| 2× MySQL 迁移测试默认 DSN（localhost:3307）不可达 | 默认 DSN 与本地实例端口（3308）不一致；显式覆盖 `TEST_MYSQL_MIGRATE_SYNC_DSN` 后 **2 passed** | 参数问题，已在 3308 实测通过 |

### 2.3 按 §17 七分类

| # | 分类 | 状态 | 说明 |
|---|---|---|---|
| 1 | 单元测试 | ✅ 通过 | Policy/事件映射/脱敏/SQL 只读/路径/恢复决策/迁移元数据等纯单元用例 |
| 2 | SQLite 集成测试 | ✅ 301 passed | 全量单进程回归（含迁移 upgrade/downgrade 往返） |
| 3 | MySQL 业务库集成 | ✅ 25 + 2 passed | 关键集 + 真实迁移实测（MySQL 8.0.46） |
| 4 | PostgreSQL Checkpointer 集成 | ✅ 25 passed | 关键集含 CP-01/02/04（AsyncPostgresSaver，PG 16.15） |
| 5 | 多进程故障恢复 | ✅ 通过 | CP-01 两进程共享 Checkpointer；CP-02 **真实 kill** 进程 A 后进程 B 接管恢复；worker crash/重启恢复用例（t 系列） |
| 6 | 真实模型 E2E | ⚠️ skipped | 无模型 Secret；3 项显式 skip，不计基础通过数（§17 要求） |
| 7 | 长稳 / 性能 | ❌ 未做 | 见 §5 未验证场景（P2 门禁项） |

## 3. Mock 与真实服务边界

| 被测对象 | Mock / 真实 | 说明 |
|---|---|---|
| LLM 模型 | **Mock**（`FakeChatModel` 脚本化 tool-call 轮次） | 确定性、无网络；真实模型见 §2.3-6 |
| 业务数据库 | 真实 SQLite（全量）/ 真实 MySQL 8.0.46（关键集+迁移） | 非内存模拟 |
| Checkpointer | 真实 AsyncSqliteSaver / 真实 AsyncPostgresSaver（PG 16） | Memory 仅误配拒绝测试 |
| Worker 租约 / 状态机 | 真实 DB CAS | 双 Worker 竞争为真实并发 |
| 外部副作用（webhook/SMTP/HTTP） | **连接级 Mock**（本地拒绝端口 127.0.0.1:1、假 SMTP server 桩、monkeypatch DNS 策略） | 真实外部服务未触达（避免真实副作用）；策略判断（SSRF/脱敏/账本状态）全部走真实代码路径并落真实 DB 断言 |
| 审批 / 取消 / reconcile API | 真实（httpx ASGI 调用 + 真实 DB） | `httpx.AsyncClient(transport=httpx.ASGITransport)` |
| 进程 | 真实子进程（CP-01/02） | 非线程模拟 |

## 4. 故障注入方式

1. **真实 kill 进程**：CP-02 子进程 A 持有执行中 Run，`kill` 后子进程 B 重新 Claim 并从 Checkpointer 恢复。
2. **崩溃点模拟**：在"工具已执行、Checkpoint 未写"等窗口抛出异常/强制结束，断言账本 `succeeded` 结果被复用、不重复执行（T-04/F-09）。
3. **连接拒绝**：外部 HTTP/SMTP 指向 `127.0.0.1:1` 真实连接失败，验证 `unknown/failed` 语义与脱敏。
4. **时间拨改**：`lease_expires_at` 拨回模拟租约过期，验证旧 Worker fencing 失效（F-13）。
5. **并发竞争**：双 Worker 同时 Claim / 双人同时提交冲突审批（CAS 单赢家，HITL-03）。
6. **重复注入**：重复审批、重复用户消息、重复写回重试（F-11/F-18/SS-02）。
7. **队列溢出 / 慢消费者**：事件订阅队列打满，断言数据库回放补齐（EV-02）。
8. **恢复决策矩阵**：Checkpoint 缺失/损坏 → 安全阻断不重放（RC-05）；`unknown` 账本 → `needs_reconciliation` 死终态，仅 reconcile API 可迁移。

## 5. 尚未验证的场景（如实标注）

1. **真实模型 E2E**（3 项 skip）：需 `AGENT_E2E_*` Secret，走 `e2e.yml` 独立通道。
2. **长稳 / 性能基准 / 长时间多 Run 并发**（P2 门禁）：未纳入本轮。
3. **MySQL Checkpointer**：不存在该形态——业务库 MySQL + Checkpointer PostgreSQL 是既定架构（`persistence.py` 支持 memory/sqlite/postgresql 三种 DSN），MySQL 仅业务库实测。
4. **F-16 Checkpoint 位级损坏**：仅覆盖账本兜底路径，位级损坏注入未测。
5. **真实 NTP 漂移 / 跨时钟漂移**：未测。
6. **DNS Rebinding 完整攻防**：应用层等价防护已测（SEC-01/02/07）；网络层 Egress Proxy 部署形态需环境侧验证。
7. **生产规模事件保留 / Ledger 归档策略执行**：策略代码已具备，长周期运行未验证。

## 6. 测试适配披露（断言零改动）

B8 引入 fail-closed 有意行为变化，仅 4 处测试 setup/夹具输入适配（详见 `phase2.1-change-log.md` §5）：`broken_tool` 显式风险登记、conftest 夹具域 allowlist、`_SlowSMTP` 对端 socket 桩、t03c 合法 webhook 夹具 URL。**所有断言保持原样，无 skip、无 xfail、无断言放宽。**

## 7. 复现命令

```powershell
# SQLite 全量（单进程，勿并发争抢共享测试库）
cd backend
$env:PYTHONPATH='.'; $env:PYTHONIOENCODING='utf-8'
python -c "import pytest,os,sys; rc=pytest.main(['tests/','-q','--tb=short','-p','no:cacheprovider']); sys.stdout.flush(); os._exit(rc)"

# PG 关键集（需 TEST_CHECKPOINT_PG_DSN / 检查点库；业务库 DSN 指向 MySQL）
$env:TEST_CHECKPOINT_PG_DSN='postgresql://user:pass@127.0.0.1:5433/agent_checkpoints'
python -c "import pytest,os,sys; rc=pytest.main(['tests/','-q','-k','p15 or p16 or t01 or t03 or t09 or t12','-p','no:cacheprovider']); sys.stdout.flush(); os._exit(rc)"

# MySQL 迁移实测
$env:TEST_MYSQL_MIGRATE_SYNC_DSN='mysql+pymysql://root:***@127.0.0.1:3308/ai_plat_migration_test'
python -m pytest tests/test_migration_mysql.py -q
```

## 8. 结论

- P0/P1 门禁相关测试在 **SQLite 全量 + MySQL 8 + PostgreSQL 16 三库**真实执行通过；
- 5 个 skip 全部为"真实模型 Secret 未配置 + 默认 DSN 端口不一致"，均已如实标注且后者已实测补过；
- 真实模型 E2E 与长稳/性能为**未完成项**（P2 与生产前置），不以任何方式宣称已验证。

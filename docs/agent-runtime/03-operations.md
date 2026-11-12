# 03-operations - 部署运行手册

> 阶段一交付文档之一（任务书 §9.4）。依赖版本、环境变量、数据库初始化、内网模型配置、
> 本地/Docker 启动与故障排查。

## 1. 依赖版本（backend/requirements.txt 锁定）

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| deepagents | 0.7.34 | Agent Harness（create_deep_agent） |
| langgraph | 1.2.20 | 图运行时/Checkpoint/Interrupt |
| langchain | 1.4.9 | Model/Tool/Middleware 抽象（langchain-core 1.7.6） |
| langchain-openai | 1.7.4 | OpenAI 兼容 ChatModel |
| langgraph-checkpoint-sqlite | 3.1.3 | 本地持久化 checkpointer |
| langgraph-checkpoint-postgres | （按需安装） | 生产多实例 checkpointer：`pip install langgraph-checkpoint-postgres` |
| openai | 3.46.0 | 既有 model_service 使用 |
| fastapi / uvicorn | 0.115.6 / 0.32.1 | API 服务 |
| sqlalchemy / alembic / aiomysql / pymysql | 2.0.36 / 1.14.0 / 0.2.0 / 1.1.1 | 业务库 MySQL |
| pytest / pytest-asyncio | 8.3.4 / 0.24.0 | 测试 |

升级约束：升级 deepagents/langgraph/langchain 前先跑 `tests/test_agent_runtime*.py`
（2026-11-09 实测兼容组合，API 变动快，勿凭示例猜签名）。

前端：`cd frontend && npm install --no-package-lock --registry=https://registry.npmjs.org`
（本机 npmmirror 镜像故障时用官方源；package-lock.json 不变）。

## 2. 环境变量（backend/app/core/config.py，均有安全默认值与类型校验）

### 2.1 服务与数据库

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `mysql+aiomysql://root:password@localhost:3307/ai_plat` | 业务库（异步） |
| `DATABASE_SYNC_URL` | `mysql+pymysql://...` | 业务库（同步/alembic） |
| `REDIS_URL` | （见 config.py） | 缓存/会话 |

### 2.2 Agent Runtime（阶段一新增，全部支持 .env 覆盖）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_RUNTIME_ENABLED` | `true` | **回滚开关**：false 回退旧 AgentEngine |
| `AGENT_MAX_STEPS` | 20 | 图执行最大步数预算 |
| `AGENT_MAX_TOOL_CALLS` | 30 | 工具调用总次数预算 |
| `AGENT_MAX_MODEL_CALLS` | 30 | 模型调用次数预算 |
| `AGENT_RUN_TIMEOUT` | 300.0 | 单次运行墙钟超时（秒），超限 → limit_exceeded |
| `AGENT_CHECKPOINT_DSN` | `memory` | `memory` / `sqlite:///./data/agent_checkpoints.db` / `postgresql+asyncpg://user:pass@host/db` |
| `AGENT_STORE_DSN` | 空 | 跨线程记忆 Store（memory/postgres；空=不启用） |
| `AGENT_ENABLE_HITL` | `true` | 高危工具人工审批（interrupt_on） |
| `AGENT_SANDBOX_MODE` | `restricted` | `restricted`/`disabled`/`external`（生产建议 disabled） |
| `AGENT_ALLOW_NETWORK` | `true` | 工具网络访问总开关 |
| `AGENT_TLS_VERIFY` | `true` | TLS 证书校验（生产禁止关闭） |
| `AGENT_CA_BUNDLE` | 空 | 内网自签 CA 证书路径 |
| `AGENT_NETWORK_ALLOWLIST` | 空 | 逗号分隔受信任主机（配置后仅放行名单，可含内网） |
| `AGENT_NETWORK_BLOCKED_CIDRS` | 空 | 逗号分隔封禁网段 |
| `AGENT_FILES_ROOT` | `./data/agent_files` | 工具文件工作区根目录（user/agent/session 三级隔离） |
| `AGENT_MAX_FILE_SIZE_MB` | 10 | 工作区单文件大小上限 |
| `AGENT_DISABLED_SKILLS` | 空 | 逗号分隔禁用的 Skill 名 |
| `AGENT_EVENT_RETENTION_DAYS` | 30 | 运行记录保留天数（事件留存接口预留） |

### 2.3 旧配置兼容

`AGENT_MAX_ITERATIONS` / `AGENT_MAX_TOOL_RETRIES` / `AGENT_TOOL_TIMEOUT_SECONDS` /
`AGENT_MAX_SAME_TOOL_CALLS` 等旧引擎配置**继续保留**（回滚路径使用），新内核预算以上表
`AGENT_MAX_*` / `AGENT_RUN_TIMEOUT` 为准；不删除旧配置，避免回滚时缺项。

## 3. 数据库初始化

1. **业务库 MySQL**：创建库（如 `ai_plat`）后执行
   ```powershell
   cd backend
   alembic upgrade head        # 含阶段一新增 agent_runs / agent_approvals（b7c8d9e0f1a2）
   ```
   迁移为纯新增表；回滚 `alembic downgrade a1b2c3d4e5f6`。禁止手工删表。
2. **Checkpoint 库**（按环境选）：
   - 本地/测试：`AGENT_CHECKPOINT_DSN=memory` 或 `sqlite:///./data/agent_checkpoints.db`
     （AsyncSqliteSaver 首次使用自动建表）；
   - 生产/多实例：部署 PostgreSQL，`pip install langgraph-checkpoint-postgres`，
     `AGENT_CHECKPOINT_DSN=postgresql+asyncpg://...`（多实例共享同一 PG）。
3. **Store（可选）**：`AGENT_STORE_DSN` 指向 postgres 启用跨线程长期记忆。
4. **文件工作区**：`AGENT_FILES_ROOT` 指向可写目录（默认 `./data/agent_files`），
   启动自动创建 user/agent/session 子目录。

## 4. 内网模型配置（OpenAI 兼容）

- 平台模型配置沿用 `ModelProvider`（base_url、api_key）与 `ModelConfig`（模型名、
  max_tokens、temperature、supports_function_call 等）表，管理界面/模型管理 API 不变。
- 兼容 LiteLLM / vLLM / Xinference 等 OpenAI 兼容网关：`base_url` 指向网关
  （如 `http://llm-gateway.internal:4000/v1`），api_key 填网关令牌。
- **硬性要求**：被 Agent 引用的模型必须支持原生 tool calling
  （`ModelConfig.supports_function_call=true`）。不满足时运行时抛 `ModelCapabilityError`
  显式报错——**绝不静默回退文本 ReAct**。部署前用 `/api/v1/models` 或管理界面核对。
- 内网 TLS：自签证书配置 `AGENT_CA_BUNDLE`；网关地址加入 `AGENT_NETWORK_ALLOWLIST`。
- 真实模型 E2E 测试环境变量：`AGENT_E2E_BASE_URL` / `AGENT_E2E_API_KEY` / `AGENT_E2E_MODEL`
  （配置后 `tests/test_agent_runtime_e2e.py` 由 skip 变为实跑）。

## 5. 启动

### 5.1 本地启动

```powershell
# 后端
cd backend
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端
cd frontend
npm install --no-package-lock --registry=https://registry.npmjs.org
npm run dev        # 开发；npm run build 产线构建
```

验证：
```powershell
cd backend
python scripts/demo_runtime.py       # 无 DB/模型依赖的最小运行时演示（多步 tool calling + HITL + SSE）
python -m pytest tests/ -q           # 全量测试
curl http://localhost:8000/api/v1/agents/tools
```

### 5.2 Docker Compose（参考骨架，按环境补全镜像与密钥）

```yaml
services:
  mysql:
    image: mysql:8.0
    environment: { MYSQL_DATABASE: ai_plat, MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD} }
    ports: ["3307:3306"]
  postgres:                              # 生产 checkpoint/store
    image: postgres:16
    environment: { POSTGRES_PASSWORD: ${PG_PASSWORD} }
  backend:
    build: ./backend
    environment:
      DATABASE_URL: mysql+aiomysql://root:${MYSQL_ROOT_PASSWORD}@mysql:3306/ai_plat
      AGENT_CHECKPOINT_DSN: postgresql+asyncpg://postgres:${PG_PASSWORD}@postgres:5432/agent_ckpt
      AGENT_SANDBOX_MODE: "disabled"
      AGENT_ENABLE_HITL: "true"
    ports: ["8000:8000"]
    depends_on: [mysql, postgres]
```

凭证一律经环境变量/密钥管理注入，**不得写进代码或样例日志**。

## 6. 故障排查

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `ModelCapabilityError: ... function call` | 模型不支持 tool calling | 换支持的模型或置 `supports_function_call=true`（勿关能力检测） |
| `ToolPolicyError` / 事件 `TOOL_FAILED: 策略拒绝` | 工具未授权 / SSRF / 路径穿越 / SQL 非只读 / 沙箱禁用 | 按 Agent 配置授权工具；内网目标加 allowlist；检查 AGENT_SANDBOX_MODE |
| `done.status=limit_exceeded` | 预算超限（步数/工具/模型次数/超时） | 调大对应 `AGENT_MAX_*`/`AGENT_RUN_TIMEOUT`，或精简任务 |
| `done.status=waiting_approval` | HITL 审批中断（正常状态，非错误） | 前端审批面板或 `POST /agents/runs/{id}/resume` 批准/拒绝 |
| `运行不存在或不在待审批状态`（resume） | 重复审批 / 运行已终态 | `GET /agents/runs/{id}` 查状态；重复 resume 不会重放副作用 |
| SSE 中断但任务仍在跑 | 客户端断连（执行继续） | 用 X-Run-Id 重查 `GET /agents/runs/{id}`；勿重复提交（用 idempotency_key） |
| sqlite checkpoint 多 Worker 行为异常 | 单机 sqlite 不支持多实例 | 生产换 `AGENT_CHECKPOINT_DSN=postgresql+asyncpg://...` |
| `InvalidTransition` 日志 | 状态机非法转移（并发竞争） | 属防御性报错；检查是否多端同时操作同一 run |
| 旧引擎行为差异 | 误开回滚开关 | 确认 `AGENT_RUNTIME_ENABLED=true` |
| 前端 build `DefaultRow` 类型告警 | 依赖版本漂移（4 个旧文件） | 与 Agent 改造无关；后续统一升级处理 |

## 7. 日志与可观测

- 核心日志 logger：`agent_runtime` / `agent_runtime.telemetry`。
- 运行/工具记录：telemetry 结构化日志（run/tool 状态、耗时、trace_id/run_id 等）；
  Langfuse/OTel 接入点见 01-architecture.md §12。
- 事件脱敏：api_key/token/password 等字段输出前置 `***`，长参数截断 1500 字符。

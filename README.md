# 企业级 AI 通用能力大平台与业务落地平台 (v2.0)

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI + SQLAlchemy (async) + Alembic + MySQL + Redis + ChromaDB |
| Agent 引擎 | ReAct 增强循环 + 滑动窗口上下文 + 结构化记忆 + 幻觉抑制 |
| 推理层 | OpenAI 兼容 API + 连接池 + 并发队列 + 30B 模型优化 |
| 前端 | Vue 3 + Element Plus + Vite + TypeScript + Pinia |
| 部署 | Docker Compose |

## 本地开发启动

### 前置条件
- Python 3.11+
- Node.js 20+
- MySQL（本地 3306 或 Docker 3308）
- Redis（可选，降级为内存模式）

### 后端

```bash
cd backend

# 1. 安装依赖
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 配置环境变量（复制并修改）
cp .env.example .env
# 修改 DATABASE_URL 中的密码

# 3. 创建数据库
python -c "import pymysql; conn=pymysql.connect(host='localhost',port=3306,user='root',password='YOUR_PWD'); conn.cursor().execute('CREATE DATABASE IF NOT EXISTS ai_plat CHARACTER SET utf8mb4'); conn.commit(); conn.close()"

# 4. 运行数据库迁移
python -m alembic upgrade head

# 5. 启动后端（开发模式）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

后端启动后访问：
- API 文档：http://localhost:8002/api/docs
- 健康检查：http://localhost:8002/api/health

### 前端

```bash
cd frontend

# 1. 安装依赖
npm install --registry https://registry.npmmirror.com

# 2. 启动开发服务器
npm run dev
```

前端默认在 http://localhost:5173 启动（端口被占用会自动递增）。

> **注意**：`vite.config.ts` 中的代理目标需与后端端口一致，默认配置为 `http://127.0.0.1:8002`。

### 默认账号
- 用户名：`admin`
- 密码：`Admin@123456`

---

## Docker Compose 一键部署

```bash
# 启动所有服务（MySQL + Redis + 后端 + 前端）
docker-compose up -d

# 查看日志
docker-compose logs -f backend

# 停止
docker-compose down
```

访问：http://localhost（前端），http://localhost:8000（后端 API）

---

## 功能模块

| 模块 | 功能 |
|---|---|
| 🔐 认证管理 | JWT 登录、Token 刷新、修改密码、RBAC 权限 |
| 🤖 模型管理 | 多供应商接入（OpenAI 兼容）、模型配置、连接测试 |
| 💬 AI 对话 | 流式对话、知识库 RAG 增强、多模型切换 |
| 📚 知识库 | 文档上传（PDF/Word/Excel/PPT/TXT）、向量化、语义检索 |
| ⚙️ 流程编排 | 可视化画布、多节点类型（AI/条件/HTTP/知识检索）、流程执行 |
| 🏪 应用市场 | 内置 10+ 行业应用模板、一键部署、应用实例管理 |
| 👥 用户管理 | 用户 CRUD、角色分配、启用/禁用 |
| 📋 审计日志 | 操作记录、多维度过滤、详情查看 |
| 🧠 **Agent 智能体** | 增强 ReAct 引擎、结构化记忆、幻觉抑制、执行轨迹追踪 |
| 📊 **性能监控** | 推理延迟、工具成功率、token 吞吐、慢请求告警 |

---

## v2.0 Agent 平台架构

### 核心增强（针对 30B 模型优化）

```
app/services/agent/               # Agent 智能体引擎包
├── engine.py                     # ReAct 核心执行引擎（增强版）
│   ├── 严格终止条件（迭代上限 + 死循环检测 + 意图漂移防护）
│   ├── 多级容错解析（正则 + 模糊匹配 + JSON 自动修复）
│   ├── 工具执行（超时 + 重试 + 权限 + 依赖）
│   ├── 幻觉抑制校验（事实核查 + 来源引用 + 自我校验）
│   └── 流式输出 + 执行轨迹记录
├── output_parser.py              # 输出解析 + JSON 修复
├── prompt_builder.py             # 30B 优化提示词工程
├── context_manager.py            # 滑动窗口 + 摘要压缩 + 关键信息置顶
├── memory_manager.py             # 短期/长期/结构化记忆（fact/rule/skill）
├── hallucination.py              # 幻觉抑制（伪造检测 + 自洽性 + 不确定性标注）
├── tool_executor.py              # 增强工具执行（超时/重试/权限/调用限制）
├── trace.py                      # 执行轨迹 + 性能监控 + 意图漂移检测
└── checkpoint.py                 # 断点续跑 + 错误恢复

app/services/inference/           # 推理层管理
├── model_manager.py              # 模型连接池 + 健康检查 + 热重载
└── queue.py                      # 请求排队 + 并发控制 + 背压机制
```

### 30B 模型十大生存线

| # | 生存线 | 实现模块 |
|---|---|---|
| 1 | 输出格式强约束（严格 JSON） | `output_parser.py` — 多级 JSON 修复 |
| 2 | 思考模板固定化 | `prompt_builder.py` — 30B 专用 ReAct 模板 |
| 3 | 滑动窗口上下文管理 | `context_manager.py` — 自动压缩 + 关键信息置顶 |
| 4 | 推理速度 ≥25 token/s | `inference/queue.py` — 并发控制 + KV Cache 配置 |
| 5 | 工具参数强校验 | `tool_executor.py` — 类型检查 + 必填校验 |
| 6 | 幻觉抑制 | `hallucination.py` — 伪造检测 + 来源引用 |
| 7 | 死循环/无限递归防护 | `engine.py` + `checkpoint.py` — 相似度检测 + 强制终止 |
| 8 | 失败重试与错误恢复 | `tool_executor.py` + `checkpoint.py` — 指数退避 + 断点续跑 |
| 9 | 记忆持久化与结构化 | `memory_manager.py` — SQLite + fact/rule/skill 分类 |
| 10 | 执行轨迹可追溯 | `trace.py` — 每步记录 + 性能统计 |

### 新增 API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/agents/memories` | GET/POST/DELETE | 结构化记忆管理（支持 memory_type 过滤） |
| `/agents/monitor/performance` | GET | Agent 引擎性能统计 |
| `/agents/monitor/inference` | GET | 推理队列 + 模型连接池状态 |
| `/agents/monitor/reload` | POST | 热重载（清空连接池和工具缓存） |

### 配置项

详见 `.env.example`，新增配置分区：
- **Agent 执行引擎** — 迭代上限、重试次数、工具超时、温度
- **推理层** — 上下文窗口、KV Cache、并发限制
- **上下文管理** — 滑动窗口轮数、摘要阈值、任务置顶
- **记忆系统** — 短期/长期容量、冲突策略、自动蒸馏
- **幻觉抑制** — 事实校验、来源引用、不确定性表达
- **监控日志** — 轨迹记录、慢请求阈值
- **稳定性** — 热重载、会话隔离、确定性模式、断点续跑

---

## 项目结构

```
AI-GeneralPlat/
├── backend/                       # FastAPI 后端
│   ├── app/
│   │   ├── api/v1/               # API 路由
│   │   ├── core/                 # 配置、数据库、安全、日志
│   │   │   ├── config.py         # 全量配置（含 Agent/推理/记忆/监控）
│   │   │   └── logging.py        # 结构化日志系统
│   │   ├── models/               # SQLAlchemy 模型
│   │   ├── schemas/              # Pydantic 模式
│   │   └── services/             # 业务逻辑
│   │       ├── agent/            # v2 Agent 智能体引擎包
│   │       ├── inference/        # 推理层管理（连接池 + 队列）
│   │       ├── tools/            # 工具注册表与执行器
│   │       ├── model_service.py  # LLM 交互层
│   │       ├── workflow_service.py
│   │       └── ...
│   ├── data/                     # 运行时数据
│   │   ├── logs/                 # 结构化日志
│   │   ├── memory/               # 记忆数据库
│   │   └── checkpoints/          # 断点续跑
│   ├── alembic/                  # 数据库迁移
│   ├── .env                      # 环境变量（本地）
│   └── requirements.txt
├── frontend/                     # Vue 3 前端
│   ├── src/
│   │   ├── api/                  # API 封装
│   │   ├── layouts/              # 布局组件
│   │   ├── router/               # 路由配置
│   │   ├── stores/               # Pinia 状态
│   │   ├── types/                # TypeScript 类型
│   │   └── views/                # 页面组件
│   └── vite.config.ts
├── docker/
│   └── mysql/init.sql
└── docker-compose.yml
```

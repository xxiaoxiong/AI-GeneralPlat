# 企业级 AI 通用能力大平台与业务落地平台

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI + SQLAlchemy (async) + Alembic + MySQL + Redis + ChromaDB |
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

---

## 项目结构

```
AI-GeneralPlat/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/v1/         # API 路由
│   │   ├── core/           # 配置、数据库、安全
│   │   ├── models/         # SQLAlchemy 模型
│   │   ├── schemas/        # Pydantic 模式
│   │   └── services/       # 业务逻辑
│   ├── alembic/            # 数据库迁移
│   ├── .env                # 环境变量（本地）
│   └── requirements.txt
├── frontend/               # Vue 3 前端
│   ├── src/
│   │   ├── api/            # API 封装
│   │   ├── layouts/        # 布局组件
│   │   ├── router/         # 路由配置
│   │   ├── stores/         # Pinia 状态
│   │   ├── types/          # TypeScript 类型
│   │   └── views/          # 页面组件
│   └── vite.config.ts
├── docker/
│   └── mysql/init.sql
└── docker-compose.yml
```

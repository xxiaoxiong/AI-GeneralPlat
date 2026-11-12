#!/bin/sh
# 生产启动入口（P0-06）：
# 1. 先执行一次且仅一次数据库迁移（alembic upgrade head）——多 Uvicorn Worker
#    不能并发迁移，迁移任务由本入口统一负责；
# 2. 迁移失败立即退出（exit 1），服务不得进入可接收请求状态；
# 3. 迁移成功后再启动 Uvicorn（多 Worker 共享 PostgreSQL Checkpointer）。
set -e

echo "[entrypoint] 执行数据库迁移: alembic upgrade head"
alembic upgrade head
echo "[entrypoint] 迁移完成"

WORKERS="${UVICORN_WORKERS:-2}"
echo "[entrypoint] 启动 uvicorn (workers=${WORKERS})"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS}"

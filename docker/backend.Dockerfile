# 构建上下文为仓库根目录（compose: context: ..），docker/ 目录集中存放部署文件
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
# PIP_INDEX_URL 可在构建时覆盖（compose 构建参数默认使用国内镜像加速）
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

COPY backend/ /app/
COPY docker/backend-entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data/uploads /app/data/chroma

EXPOSE 8000

# 启动入口 entrypoint.sh：先执行 alembic upgrade head（失败即退出），再启动
# Uvicorn 多 Worker。多 Worker 必须共享 PostgreSQL Checkpointer
# （AGENT_CHECKPOINT_DSN=postgresql://...，由 compose 注入，禁止 memory）。
CMD ["/app/entrypoint.sh"]

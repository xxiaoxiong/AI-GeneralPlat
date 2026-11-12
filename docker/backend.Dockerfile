FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data/uploads /app/data/chroma

EXPOSE 8000

# 启动前执行数据库迁移（alembic head = f1a2b3c4d5e6），失败则不启动；
# 单 worker：Checkpoint 使用 SQLite（AGENT_CHECKPOINT_DSN），多进程共享 SQLite 不被支持
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]

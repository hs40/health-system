# 健康运动管理系统 · 后端镜像（路线 C：全 Python）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 时区（日志与调度口径）
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用层缓存）
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel

COPY app ./app
# Alembic 配置与迁移脚本（T1.1）：容器内可执行 alembic upgrade head
COPY alembic.ini ./
RUN pip install -e .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/ops/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

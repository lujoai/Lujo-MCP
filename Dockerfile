FROM python:3.12-slim

WORKDIR /app

# 先装依赖，利用层缓存。
# FIX: P2 安装锁定的 requirements-locked.txt（固定传递依赖版本，构建可复现）
COPY requirements-locked.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝源码（PostgreSQL migrations 已随 Step 3 WP6 归档至 archive/pg-migrations/，
# 不再进入运行时镜像）
COPY app ./app
COPY browser-sdk ./browser-sdk
COPY examples ./examples
COPY scripts ./scripts

# FIX: P2 非 root 运行 —— 默认 root 运行违背最小权限原则，容器逃逸风险高
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# 暴露端口
EXPOSE 8000

# 启动
CMD ["python", "-m", "app.main"]

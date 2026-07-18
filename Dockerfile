FROM python:3.12-slim

# 系统依赖（psycopg2 需要 libpq）
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝源码
COPY app ./app
COPY browser-sdk ./browser-sdk
COPY examples ./examples
COPY migrations ./migrations
COPY scripts ./scripts

# 暴露端口
EXPOSE 8000

# 启动
CMD ["python", "-m", "app.main"]

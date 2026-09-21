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

# W10 / P2-SEC-1：app/config.py 的 host 默认值已收紧为 127.0.0.1（单用户本地
# 定位）。容器内必须绑 0.0.0.0，否则服务只监听容器回环 —— 端口发布（-p /
# ports）与容器间访问全部打不通，而 healthcheck 在容器内执行仍会显示健康，
# 故障极难发现。对外暴露面由发布地址控制（两份 compose 都只发布到 127.0.0.1），
# 需要改变监听地址时在 compose 的 environment 里覆盖本值即可。
ENV HOST=0.0.0.0

# 启动
CMD ["python", "-m", "app.main"]

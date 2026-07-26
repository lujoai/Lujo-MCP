# PostgreSQL 本地连接修复指南

## 结论

本轮已确认，项目之前的 PostgreSQL 阻塞**不是服务端配置异常**，而是：

1. 本地 `.env` 中 `PG_PASSWORD` 与当前 PostgreSQL 实际密码不一致
2. PostgreSQL 日志记录为 `用户 "postgres" Password 认证失败`
3. 在修正凭据后，以下链路均已恢复：
   - `psql`
   - `psycopg2`
   - `asyncpg`
   - `tests/integration/test_runtime_enablement.py -k postgresql`
   - `tests/integration/test_runtime_enablement.py -k asyncpg`
   - `tests/integration/test_pg_integration.py`

## 推荐修复步骤

### 方案 1：同步本地 `.env` 中的 PG 凭据

把本机实际可用的 PostgreSQL 参数同步到 `.env`：

```env
STORAGE_BACKEND=postgresql
PG_HOST=localhost
PG_PORT=5432
PG_DATABASE=ai_debug_mcp
PG_USER=postgres
PG_PASSWORD=你的当前 PostgreSQL 密码
```

如果需要异步存储，同时开启：

```env
PG_ASYNC_ENABLED=true
```

### 方案 2：先用 `psql` 验证凭据，再跑项目测试

```powershell
$env:PGPASSWORD='你的当前 PostgreSQL 密码'
& 'C:\Program Files\PostgreSQL\16\bin\psql.exe' -h localhost -U postgres -d postgres -c "SELECT current_user, current_database();"
```

若这一步能成功，再继续跑项目测试。

## 验证命令

```powershell
python -m pytest tests/integration/test_runtime_enablement.py -q -k postgresql
python -m pytest tests/integration/test_runtime_enablement.py -q -k asyncpg
python -m pytest tests/integration/test_pg_integration.py -q
```

## 如果再次失败，按这个顺序排查

1. 核对 `.env` / 本地环境变量中的 `PG_HOST`、`PG_PORT`、`PG_DATABASE`、`PG_USER`、`PG_PASSWORD`
2. 用 `psql` 或 pgAdmin 验证同一组凭据能否成功登录
3. 查看 PostgreSQL 日志，确认是否仍然是认证失败
4. 只有在凭据确认无误后，才继续排查 `pg_hba.conf`、`postgresql.conf`、SSL 或编码问题

## 当前项目状态

✅ 已完成：

- 文档收口和功能矩阵
- MCP HTTP 流式闭环
- Playwright UI 验证
- Redis L2 缓存验证
- OpenTelemetry 验证
- 熔断器恢复验证
- PostgreSQL / asyncpg 本机真实链路验证

🟡 仍待环境支持：

- Docker daemon 启动后的容器化复现实验

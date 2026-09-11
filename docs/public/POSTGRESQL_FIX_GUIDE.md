# PostgreSQL 本地连接修复指南

> **v0.8.0（2026-09-11）**：KB 经验默认走本地 SQLite「笔记本」，不再依赖 PostgreSQL 即可跨重启沉淀。`STORAGE_BACKEND=memory` 仍为默认；PostgreSQL 需显式启用，现为**实验性后端：不承诺支持，存在已知未修问题**（见下节）。产品定位为单用户本地自用，不承诺中央多人共享 PostgreSQL。
>
> 上一版 v0.7.9 已发布（2026-09-10）：asyncpg errors 真库读写链路、`(fingerprint, session_id)` 节流键和跨事件循环 pool 生命周期已验证（范围限于当时的 errors 链路，不代表全部 PG 路径已验证）。

## PostgreSQL 实验性后端与已知限制

PostgreSQL 是**显式可选的实验性后端，不承诺支持**。单用户本地自用的推荐配置是零配置默认组合：

- 运行现场：`STORAGE_BACKEND=memory`（默认），traces/errors/sessions/specs 在进程内采集；
- KB 调试经验：默认写穿工作目录下的本地 SQLite「笔记本」（`lujo-kb.sqlite3`，可用 `KB_PERSIST_ENABLED=false` 关闭），零安装、进程重启自动回灌。

SQLite 笔记本**只持久化 KB 经验**，不持久化 traces/errors/sessions/specs，不是全量运行现场的持久化替代。

选择 `STORAGE_BACKEND=postgresql` 即表示接受该实验性路径；后续版本会在进程启动时输出一条实验性 warning（当前版本尚未实现该提示，使用前请先通读本节）。已知未修问题包括：

1. **KB 延迟初始化失败可能无法降级**：存储工厂仅在 store 构造期处理部分异常；`_ensure_init` 延迟到首次读写才建表/连库，该阶段失败不再经过工厂 fallback，KB 可能持续抛错——不能保证「自动降级纯内存、主流程零影响、服务照常启动」。
2. **错误计数可能偏低**：同指纹/同会话短时间窗口内的重复错误经节流只发送一次快照，PG 历史中的 `occurrence_count` 可能低于真实发生次数。
3. **调度失败后的节流记账可能抑制有效写入**：无事件循环或调度任务创建失败后仍可能记录节流时间，随后的有效写入会被错误跳过。
4. **stdio 模式 asyncpg 连接池关闭路径尚未验证**：极端情况下可能影响进程退出或连接回收。

迁移与退役：PG → SQLite 笔记本的迁移工具与正式移除 PG 的计划在后续版本提供；当前版本**不会自动迁移** PG 中的 KB 数据，也不会删除或忽略既有 PG 配置。继续使用 PG 前请自行备份数据。连接、凭据与认证排障见下文各节。

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
PG_DATABASE=lujo_mcp
PG_USER=postgres
PG_PASSWORD=你的当前 PostgreSQL 密码
```

如果需要异步存储，同时开启：

```env
PG_ASYNC_ENABLED=true
```

### 方案 2：先用 `psql` 验证凭据，再跑项目测试

```powershell
$env:PGPASSWORD='your-postgres-password'
psql -h localhost -U postgres -d postgres -c "SELECT current_user, current_database();"
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

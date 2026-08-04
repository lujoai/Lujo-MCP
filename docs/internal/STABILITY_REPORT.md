# ai-debug-mcp 稳定性验证报告

> 本报告记录“代码已写”到“可交付启用”的收口状态。  
> 判定维度分为：`已验证`、`部分验证`、`待环境验证`。

## 一、本轮执行的验证范围

本轮优先核查以下五类能力：

1. PostgreSQL / asyncpg 存储链路
2. Playwright UI verify / auto_test
3. Redis L1/L2 缓存与共享状态
4. LLM / PG 熔断器
5. OpenTelemetry 导出链路

### 本轮已执行测试

| 命令 | 结果摘要 |
| --- | --- |
| `python -m pytest tests/unit/test_mcp_routes.py -q` | 通过，覆盖 ready 推送、SSE 结果桥接、DELETE 清理订阅 |
| `python -m pytest tests/unit/test_otel.py tests/unit/test_circuit_breaker.py tests/integration/test_mcp_verify_ui.py -q` | 通过，覆盖 OTel、熔断器、verify_ui 协议链路 |
| `python -m pytest tests/integration/test_api.py -q` | 通过，SSE 长连接草案用例仍为 skip |
| `PG_HOST=localhost PG_PORT=5432 PG_DATABASE=ai_debug_mcp PG_USER=postgres PG_PASSWORD=<本机有效密码> STORAGE_BACKEND=postgresql python -m pytest tests/integration/test_pg_integration.py -q` | 通过（15 passed / 1 skipped），说明 PG 集成链路已打通 |
| `python -m pytest tests/unit/test_storage.py tests/unit/test_analyzer.py tests/unit/test_dashboard.py -q` | 通过，含少量环境依赖型 skip |
| `python -m pytest tests/integration/test_runtime_enablement.py -q` | 默认环境全部 skip，说明运行时 smoke tests 不会污染常规开发流 |
| `PG_HOST=localhost PG_PORT=5432 PG_DATABASE=ai_debug_mcp PG_USER=postgres PG_PASSWORD=<本机有效密码> STORAGE_BACKEND=postgresql python -m pytest tests/integration/test_runtime_enablement.py -q -k postgresql` | 通过，验证 `psycopg2` PG 运行时 smoke test |
| `PG_HOST=localhost PG_PORT=5432 PG_DATABASE=ai_debug_mcp PG_USER=postgres PG_PASSWORD=<本机有效密码> STORAGE_BACKEND=postgresql PG_ASYNC_ENABLED=true python -m pytest tests/integration/test_runtime_enablement.py -q -k asyncpg` | 通过，验证 `asyncpg` 运行时 smoke test |
| `STATE_BACKEND=redis REDIS_URL=redis://127.0.0.1:6379/0 pytest tests/integration/test_runtime_enablement.py -q -k redis` | 通过，验证本地 Redis 状态后端 smoke test |
| `python -m pytest tests/integration/test_redis_cache_integration.py -q` | 通过，验证 LLM L2 缓存与 Dashboard L2 缓存的真实 Redis 回填链路 |
| `OTEL_ENABLED=true pytest tests/integration/test_runtime_enablement.py -q -k otel` | 通过，`/metrics` 兼容链路正常；exporter 不可达时出现重试日志 |
| `python -m pytest tests/integration/test_otel_collector_integration.py -q` | 通过，验证 OTel exporter 向本地 gRPC collector 的真实导出链路 |
| `CIRCUIT_BREAKER_ENABLED=true pytest tests/integration/test_runtime_enablement.py -q -k circuit` | 通过，运行时 breaker 实例可正常创建 |
| `python -m pytest tests/integration/test_circuit_breaker_recovery.py -q` | 通过，验证 LLM / PG breaker 在故障后可按 `open -> half-open -> close` 恢复 |
| `python -m pytest tests/integration/test_ui_verify_live.py -q` | 通过，验证 Playwright + Chromium + 本地 HTTP 页面 + DOM 断言的真实浏览器链路 |
| `python -m pytest tests/e2e/test_sdk_full_chain.py -v` | 通过（5 passed, 1 skipped），验证 Browser SDK V3/V6 端到端全链路：demo 页面可访问、SDK 加载、trace_id 贯穿、/ingest/batch 批量入库 |
| `python -m pytest tests/e2e/test_sdk_v5_enhancements.py -v` | 通过（4 passed），验证 Browser SDK V5 增强功能：gzip 压缩传输（压缩率 92.7%）、节流控制、localStorage 失败降级 |

> 本轮已顺手修复两类测试质量告警：`pytest_asyncio` 的 loop scope 已显式配置，`app/config.py` 中的 `model_fields` 访问也已改为类级访问。
> 
> **E2E 联调修复记录（2026-07-25）**：
> - 新增 `/demo/silent-failure` 路由，补齐静默失败演示页面入口
> - Demo 页面 SDK 配置补齐 `apiKey` 字段，适配鉴权中间件
> - `silent_failure_demo.html` 中 SDK 路径从相对路径 `../../browser-sdk/ai-debug.js` 改为绝对路径 `/ai-debug.js`
> - `AuthMiddleware.PUBLIC_PATHS` 新增豁免：`/demo`、`/demo/silent-failure`、`/ai-debug.js`
> - **SDK V6 修复**：`_armUISilentFailureDetection` 延迟 100ms 再开始观察，避免点击本身的 DOM 变化（focus、:active）被误判
> - **Demo 页面修复**：`silentButton` 点击后不再修改 DOM，真正模拟"假装提交但不更新 UI"场景
> - SDK 导出新增调试接口：`_getPendingUISilentFailure()`、`_getLastDomMutationAt()`、`_getUIMutationObserver()`
> 
> **P1 SDK V5 增强补齐（2026-07-25）**：
> - **gzip 压缩传输**：使用浏览器原生 `CompressionStream` API，payload > 4KB 自动压缩（实测压缩率 92.7%）
> - **节流控制**：5秒窗口内最多发送 2 批，超过限制的批次延迟到窗口结束后发送
> - **失败降级**：超过重试次数后自动暂存到 localStorage，下次 SDK 初始化时自动恢复并重试
> - **服务端支持**：`/ingest/batch` 端点支持 `Content-Encoding: gzip` 头，自动解压并解析 JSON
> - **SDK 版本升级**：v0.4.0 → v0.5.0
> - **新增配置项**：`enableCompression`、`compressionThreshold`、`throttleWindowMs`、`maxBatchesPerWindow`、`enableLocalStorageFallback`、`localStorageKey`、`maxPendingBatches`

### 本轮运行时连通性证据

| 能力 | 运行结果 | 说明 |
| --- | --- | --- |
| 本机 PostgreSQL 端口探测 | `127.0.0.1:5432` 可达 | 说明本地存在 PG 服务或端口占用 |
| PostgreSQL 握手探测 | 返回 `SCRAM-SHA-256` 认证请求 | 说明对端确实是 PostgreSQL，且认证阶段已进入 SCRAM |
| `psql` 直连本机 PG | 通过：`current_user=postgres`、`current_database=postgres` | 说明服务端认证链路正常，原问题不在 PostgreSQL 本身 |
| `psycopg2` 直连当前配置 PG | 通过：`SELECT 1` 返回正常 | 原异常来自本地 `.env` 中 PG 密码与当前 PostgreSQL 实际密码不一致 |
| `asyncpg` 运行时 smoke test | 通过 | 说明 asyncpg 链路在修正凭据后可正常完成握手与查询 |
| 本机 PG 日志核查 | 记录为 `用户 "postgres" Password 认证失败` | 说明此前报错的根因是凭据错误，不是服务端编码或认证机制异常 |
| 本机 Redis 端口探测 | 初始不可达；手动启动 `redis-server --port 6379 --appendonly no` 后可达 | 已验证 Redis 本地运行路径可行 |
| Redis 运行时 smoke test | 通过 | `STATE_BACKEND=redis` 下状态后端可真实连接、计数与限流 |
| 本机 OTLP Collector 端口探测 | 默认 `127.0.0.1:4317` 不可达 | 默认环境下仍无常驻 collector |
| OTel 运行时 smoke test | 通过（在默认无 collector 时伴随 exporter 连接拒绝日志） | 说明 OTel 启用不影响 `/metrics` 与应用指标链路 |
| OTel 本地 collector 集成测试 | 通过 | 说明 exporter -> gRPC collector 真实链路已验证 |
| Docker Compose 启动 `postgres`/`redis` | 失败：Docker daemon 未启动 | 当前机器只有 Docker CLI，不具备容器化验证条件 |

## 二、当前验证结论

| 能力 | 当前结论 | 代码依据 | 已有测试依据 | 仍需补充 |
| --- | --- | --- | --- | --- |
| PostgreSQL Store | 已验证 | `app/mcp/core/storage/pg_store.py` | `tests/unit/test_storage.py` `tests/integration/test_pg_integration.py` `tests/integration/test_runtime_enablement.py` | 已确认本机 PG 可用，之前阻塞点为 `.env` 凭据错误；代码链路与集成测试均已打通 |
| asyncpg Store | 已验证 | `app/mcp/core/storage/async_pg_store.py` | `tests/unit/test_storage.py` `tests/integration/test_runtime_enablement.py` | 已确认修正凭据后运行时 smoke test 通过 |
| Playwright UI verify | 已验证 | `app/mcp/verifier/ui_runner.py` `app/mcp/tools/verify_ui_api.py` | `tests/unit/test_ui_runner.py` `tests/integration/test_mcp_verify_ui.py` `tests/integration/test_ui_verify_live.py` | 真实浏览器 + 本地页面 + DOM 断言链路已跑通；业务页面可按项目需要继续补场景 |
| L1/L2 缓存 | 已验证 | `app/llm/analyzer.py` `app/api/dashboard.py` | `tests/unit/test_analyzer.py` `tests/unit/test_dashboard.py` `tests/integration/test_runtime_enablement.py` `tests/integration/test_redis_cache_integration.py` | Redis 状态后端、LLM L2 缓存回填、Dashboard L2 回读均已验证 |
| 熔断器 | 已验证 | `app/llm/analyzer.py` `app/mcp/core/storage/pg_store.py` | `tests/unit/test_circuit_breaker.py` `tests/integration/test_runtime_enablement.py` `tests/integration/test_circuit_breaker_recovery.py` | 已验证 breaker 实例启用与 `open -> half-open -> close` 恢复链路 |
| OpenTelemetry | 已验证 | `app/observability.py` `app/main.py` | `tests/unit/test_otel.py` `tests/integration/test_runtime_enablement.py` `tests/integration/test_otel_collector_integration.py` | 已验证 `/metrics` 兼容链路与 exporter -> gRPC collector 真实导出链路 |

## 三、启用前提

| 能力 | 启用前提 |
| --- | --- |
| PostgreSQL | `STORAGE_BACKEND=postgresql`，并提供 PG 连接参数 |
| asyncpg | 在 PostgreSQL 基础上启用 `PG_ASYNC_ENABLED=true` |
| Playwright UI verify | 安装 `playwright` 并执行 `playwright install chromium` |
| Redis L2 缓存 / 共享限流 | 提供 `REDIS_URL`，多实例下建议 `STATE_BACKEND=redis` |
| 熔断器 | `CIRCUIT_BREAKER_ENABLED=true`，并配置阈值参数 |
| OpenTelemetry | `OTEL_ENABLED=true`，如需导出则配置 `OTEL_EXPORTER_ENDPOINT` |

## 四、建议测试命令

```bash
python -m pytest tests/unit/test_mcp_routes.py -q
python -m pytest tests/unit/test_ui_runner.py -q
python -m pytest tests/unit/test_circuit_breaker.py -q
python -m pytest tests/unit/test_otel.py -q
python -m pytest tests/integration/test_mcp_verify_ui.py -q
python -m pytest tests/integration/test_ui_verify_live.py -q
python -m pytest tests/integration/test_pg_integration.py -q
python -m pytest tests/integration/test_runtime_enablement.py -q
python -m pytest tests/integration/test_redis_cache_integration.py -q
python -m pytest tests/integration/test_otel_collector_integration.py -q
python -m pytest tests/integration/test_circuit_breaker_recovery.py -q
```

## 五、后续验证任务

| ID | 任务 | 目标 | 状态 |
| --- | --- | --- | --- |
| STAB-007 | Docker 容器化验证 | 在 Docker daemon 可用时补 PostgreSQL / Redis / OTel 的容器化复现实验 | 🔲 待环境具备 |
| STAB-002 | Playwright 实浏览器验证 | 覆盖 URL allowlist、安全限制、交互断言 | ✅ 已完成 |
| STAB-003 | Redis L2 集成验证 | 覆盖命中、失效、回填、共享限流 | ✅ 已完成 |
| STAB-004 | OTel exporter smoke test | 覆盖启动、导出失败降级、优雅关闭 | ✅ 已完成 |
| STAB-005 | 熔断恢复测试 | 覆盖 open / half-open / close 状态切换 | ✅ 已完成 |

## 六、小范围发布前回归（2026-07-25）

本轮在完成环境固化后，额外执行了一次发布前小范围回归，目标是确认最关键的交付链路在当前本机环境下仍然保持可用。

### 本轮回归覆盖

| 回归范围 | 命令 | 结果摘要 |
| --- | --- | --- |
| SSE / MCP 核心协议 | `python -m pytest tests/unit/test_mcp_routes.py tests/integration/test_api.py -q` | 通过，`MCP routes` 与 HTTP API 回归均正常；`test_api.py` 中草案型 SSE 用例仍为既有 skip |
| PostgreSQL 集成 | `PG_HOST=localhost PG_PORT=5432 PG_DATABASE=ai_debug_mcp PG_USER=postgres PG_PASSWORD=<本机有效密码> STORAGE_BACKEND=postgresql python -m pytest tests/integration/test_pg_integration.py -q` | 通过（`15 passed / 1 skipped`），确认 PG 集成链路仍稳定 |
| PostgreSQL / asyncpg smoke test | `PG_HOST=localhost PG_PORT=5432 PG_DATABASE=ai_debug_mcp PG_USER=postgres PG_PASSWORD=<本机有效密码> STORAGE_BACKEND=postgresql PG_ASYNC_ENABLED=true python -m pytest tests/integration/test_runtime_enablement.py -q -k "postgresql or asyncpg"` | 通过，`psycopg2` 与 `asyncpg` 运行时探测均正常 |
| Redis 关键链路 | `STATE_BACKEND=redis REDIS_URL=redis://127.0.0.1:6379/0 python -m pytest tests/integration/test_runtime_enablement.py tests/integration/test_redis_cache_integration.py -q -k "redis"` | 通过，状态后端与 Redis L2 缓存回填链路正常 |
| OTel / 熔断器单元验证 | `python -m pytest tests/unit/test_otel.py tests/unit/test_circuit_breaker.py -q` | 通过，默认配置与核心逻辑断言正常 |
| OTel / 熔断器集成验证 | `OTEL_ENABLED=true CIRCUIT_BREAKER_ENABLED=true python -m pytest tests/integration/test_runtime_enablement.py tests/integration/test_otel_collector_integration.py tests/integration/test_circuit_breaker_recovery.py -q -k "otel or circuit"` | 通过；默认无 collector 时出现 exporter 重试日志，属于预期降级表现 |
| UI verify | `python -m pytest tests/integration/test_mcp_verify_ui.py tests/integration/test_ui_verify_live.py -q` | 通过，协议层 verify 与真实浏览器链路均正常 |

### 本轮回归结论

1. `SSE / MCP 核心协议`、`PG / asyncpg`、`Redis`、`OTel`、`熔断恢复`、`UI verify` 六类关键交付链路均已再次验证通过。
2. `OTEL_ENABLED=true` 场景下若本机没有常驻 collector，会看到 `localhost:4317` 连接拒绝与重试日志；该现象已由集成测试证明不会阻断 `/metrics` 和整体启动流程。
3. 本轮未发现新的功能性回归；当前剩余环境侧缺口仍是 `STAB-007`，即等待 Docker daemon 可用后补容器化复现实验。

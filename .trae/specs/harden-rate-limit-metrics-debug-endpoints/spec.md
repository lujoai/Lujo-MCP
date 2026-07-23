# 限流原子化、/metrics 鉴权、诊断端点清理 Spec

## Why
当前存在四个安全/可靠性问题：
1. Redis 限流的 `INCR` + `EXPIRE` 非原子操作，进程崩溃可能导致 key 永不过期、限流永久失效
2. Redis 异常时限流降级放行（fail-open），攻击者可利用此特性绕过限流
3. `/metrics` 端点免鉴权，暴露内部运行指标；path label 未模板化，存在基数爆炸风险
4. `/api/debug/echo` 和 `/api/debug/token` 诊断端点在生产环境不应暴露，缺少开关控制

## What Changes
- `app/state/store.py`：Redis `allow()` 改用 Lua 脚本原子化 INCR+EXPIRE；异常改为 fail-closed（拒绝请求）
- `app/middleware.py`：从 `PUBLIC_PATHS` 移除 `/metrics`，使其需要鉴权
- `app/observability.py`：path label 改为路由模板格式，增加特殊字符转义
- `app/api/debug.py`：`/echo` 和 `/token` 端点增加 `DEBUG_ENDPOINTS_ENABLED` 环境变量开关（默认关闭）
- `app/config.py`：新增 `debug_endpoints_enabled` 配置项
- `tests/unit/test_middleware.py`：新增限流相关单元测试

## Impact
- Affected specs: 无直接关联的已有 spec
- Affected code:
  - `app/state/store.py` — RedisStateStore.allow()
  - `app/middleware.py` — AuthMiddleware.PUBLIC_PATHS
  - `app/observability.py` — MetricsMiddleware path 标签处理
  - `app/api/debug.py` — debug_echo, debug_token 端点
  - `app/config.py` — 新增配置项
  - `tests/unit/test_middleware.py` — 新建测试文件

## ADDED Requirements

### Requirement: Redis 限流原子化
RedisStateStore 的 `allow()` 方法 SHALL 使用 Lua 脚本将 INCR + EXPIRE 合并为原子操作。

#### Scenario: 正常限流
- **WHEN** Redis 可用且请求未超限
- **THEN** Lua 脚本原子执行 INCR + EXPIRE，返回放行

#### Scenario: 请求超限
- **WHEN** 计数超过 limit
- **THEN** 返回拒绝（429）

### Requirement: 限流 fail-closed
RedisStateStore 的 `allow()` 方法在 Redis 异常时 SHALL 拒绝请求（fail-closed），而非放行。

#### Scenario: Redis 不可用
- **WHEN** Redis 连接异常或超时
- **THEN** `allow()` 返回 `False`，记录 `logger.error`，请求被限流中间件返回 429

### Requirement: /metrics 端点鉴权
`/metrics` 路径 SHALL 从 `AuthMiddleware.PUBLIC_PATHS` 中移除，访问需要有效的 API Key。

#### Scenario: 无 API Key 访问 /metrics
- **WHEN** 请求 `/metrics` 且未携带有效 API Key
- **THEN** 返回 401 Unauthorized

#### Scenario: 携带有效 API Key 访问 /metrics
- **WHEN** 请求 `/metrics` 且携带有效 API Key
- **THEN** 正常返回 Prometheus 指标数据

### Requirement: path label 模板化
MetricsMiddleware SHALL 将请求路径归一化为路由模板格式（如 `/trace/{id}`），并对 label 值进行特殊字符转义。

#### Scenario: 路径包含动态参数
- **WHEN** 请求路径为 `/api/debug/run/abc123`
- **THEN** 指标 label 中的 path 为模板格式，而非原始路径

#### Scenario: label 值包含特殊字符
- **WHEN** 路径中包含换行符或其他特殊字符
- **THEN** label 值中特殊字符被移除或转义

### Requirement: 诊断端点环境开关
`/api/debug/echo` 和 `/api/debug/token` SHALL 受 `DEBUG_ENDPOINTS_ENABLED` 环境变量控制，默认关闭。

#### Scenario: 开关关闭（默认）
- **WHEN** `DEBUG_ENDPOINTS_ENABLED` 为 `False`（默认值）
- **THEN** `/api/debug/echo` 和 `/api/debug/token` 返回 404

#### Scenario: 开关开启
- **WHEN** `DEBUG_ENDPOINTS_ENABLED` 为 `True`
- **THEN** 端点正常可用，仍需 API Key 鉴权

## MODIFIED Requirements

### Requirement: AuthMiddleware PUBLIC_PATHS
`AuthMiddleware.PUBLIC_PATHS` SHALL 仅包含 `/` 和 `/health`，移除 `/metrics`。

#### Scenario: 公开路径列表
- **WHEN** 检查 `PUBLIC_PATHS`
- **THEN** 仅包含 `("/", "/health")`

## REMOVED Requirements
无

# Checklist

- [x] `RedisStateStore.allow()` 使用 Lua 脚本原子化 INCR + EXPIRE，不再分两步调用
- [x] `RedisStateStore.allow()` 异常时返回 `False`（fail-closed）并调用 `logger.error` 记录错误
- [x] `AuthMiddleware.PUBLIC_PATHS` 仅包含 `("/", "/health")`，不包含 `/metrics`
- [x] `MetricsMiddleware` 使用路由模板（如 `/api/debug/run`）而非原始 URL path 作为指标 label
- [x] path label 值对换行符等特殊字符进行了转义/移除处理
- [x] `config.py` 新增 `debug_endpoints_enabled: bool = False` 配置项
- [x] `/api/debug/echo` 端点在 `debug_endpoints_enabled=False` 时返回 404
- [x] `/api/debug/token` 端点在 `debug_endpoints_enabled=False` 时返回 404
- [x] `.env.example` 中包含 `DEBUG_ENDPOINTS_ENABLED=False` 配置项
- [x] `tests/unit/test_middleware.py` 存在且包含限流相关测试用例
- [x] 限流测试覆盖：正常放行、超限拒绝、Redis 异常 fail-closed
- [x] 所有现有测试仍然通过

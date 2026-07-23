# Tasks

- [x] Task 1: Redis 限流 Lua 脚本原子化 + fail-closed
  - [x] SubTask 1.1: 在 `app/state/store.py` 的 `RedisStateStore` 中新增 `_atomic_incr_with_expire()` 方法，使用 Lua 脚本将 INCR + EXPIRE 合并为原子操作
  - [x] SubTask 1.2: 修改 `allow()` 方法调用 `_atomic_incr_with_expire()`，异常时改为 `return False`（fail-closed）+ `logger.error`
  - [x] SubTask 1.3: 确保文件顶部有 `logging` 和 logger 实例

- [x] Task 2: /metrics 鉴权 — 从 PUBLIC_PATHS 移除
  - [x] SubTask 2.1: 在 `app/middleware.py` 中将 `AuthMiddleware.PUBLIC_PATHS` 从 `("/", "/health", "/metrics")` 改为 `("/", "/health")`

- [x] Task 3: observability.py path label 模板化 + 转义
  - [x] SubTask 3.1: 在 `app/observability.py` 的 `MetricsMiddleware` 中新增路径模板化逻辑：利用 FastAPI 的 `request.scope["route"]` 获取路由路径模板（如 `/trace/{id}`），无匹配路由时使用原始路径
  - [x] SubTask 3.2: 对 path label 值增加转义处理：移除换行符 `\n`、`\r` 等特殊字符

- [x] Task 4: 诊断端点环境变量开关
  - [x] SubTask 4.1: 在 `app/config.py` 中新增 `debug_endpoints_enabled: bool = False` 配置项
  - [x] SubTask 4.2: 在 `app/api/debug.py` 中为 `/echo` 和 `/token` 端点添加 `settings.debug_endpoints_enabled` 检查，关闭时返回 404
  - [x] SubTask 4.3: 在 `.env.example` 中添加 `DEBUG_ENDPOINTS_ENABLED=False` 配置项说明

- [x] Task 5: 新增限流单元测试
  - [x] SubTask 5.1: 新建 `tests/unit/test_middleware.py`，编写 RedisStateStore.allow() 的单元测试：正常限流、超限拒绝、Lua 脚本原子性验证、fail-closed 行为验证
  - [x] SubTask 5.2: 编写 MemoryStateStore.allow() 的对照测试

# Task Dependencies
- Task 5 依赖 Task 1（测试需要验证 Lua 脚本和 fail-closed 逻辑）
- Task 2、3、4 互相独立，可并行执行
- Task 1 独立，可与其他任务并行

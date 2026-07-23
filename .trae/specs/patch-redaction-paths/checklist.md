# Checklist

- [x] exception_hook.py: `_hook` 中 `capture_exception` 返回的 `message` 和 `traceback` 字段经过 `redact()` 处理
- [x] exception_hook.py: `_asyncio_handler` 中 `capture_exception` 返回的 `message` 和 `traceback` 字段经过 `redact()` 处理
- [x] exception_hook.py: 已添加 `from app.mcp.core.redaction import redact` 导入
- [x] stacktrace.py: `format_trace_for_ai` 返回的文本经过 `redact()` 处理
- [x] redaction.py: `redact()` 在 `redaction_enabled=False` 时输出 `logger.warning` 告警
- [x] config.py: `redaction_enabled` 字段注释明确强调生产环境必须为 True
- [x] ai-debug.js: 定义内置默认敏感键名列表 `_DEFAULT_REDACT_FIELDS`，包含 cookie/access_token/api_key 等
- [x] ai-debug.js: `_redact` 对字符串值尝试 JSON.parse 深度脱敏
- [x] ai-debug.js: `_redact` 键名匹配大小写不敏感
- [x] ai-debug.js: `redactFields` 为空时回退使用内置默认列表
- [x] test_redaction.py: 新增 `test_capture_exception_message_redacted` 测试通过
- [x] test_redaction.py: 新增 `test_capture_exception_traceback_redacted` 测试通过
- [x] test_redaction.py: 新增 `test_format_trace_for_ai_redacted` 测试通过
- [x] 所有现有测试仍然通过（无回归）

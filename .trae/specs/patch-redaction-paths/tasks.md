# Tasks

- [x] Task 1: exception_hook.py — capture_exception 后对 message/traceback 调 redact()
  - [x] SubTask 1.1: 在 `_hook` 中，`capture_exception` 返回 exc_data 后，对 `exc_data["message"]` 和 `exc_data["traceback"]` 调用 `redact()` 再传给 `record_error`
  - [x] SubTask 1.2: 在 `_asyncio_handler` 中做同样处理
  - [x] SubTask 1.3: 添加 `from app.mcp.core.redaction import redact` 导入

- [x] Task 2: stacktrace.py — format_trace_for_ai 增加 redact() 调用
  - [x] SubTask 2.1: 在 `format_trace_for_ai` 的 `return` 处对拼接结果调用 `redact()` 后返回

- [x] Task 3: redaction.py — redaction_enabled=False 时增加 logger.warning
  - [x] SubTask 3.1: 在 `redact()` 函数中 `if not settings.redaction_enabled` 分支内增加 `logger.warning("脱敏已关闭(redaction_enabled=False)，敏感信息将不被掩码")` 后 return

- [x] Task 4: config.py — 注释强调生产环境必须 redaction_enabled=True
  - [x] SubTask 4.1: 在 `redaction_enabled` 字段上方增加醒目注释，强调生产环境禁止设为 False

- [x] Task 5: ai-debug.js — _redact 改为递归深度脱敏
  - [x] SubTask 5.1: 定义内置默认敏感键名列表 `_DEFAULT_REDACT_FIELDS`（含 password, token, secret, authorization, cookie, access_token, api_key, passwd, pwd 等）
  - [x] SubTask 5.2: 修改 `_redact` 函数：对字符串值尝试 `JSON.parse` → 递归 `_redact` → `JSON.stringify` 回写；解析失败则保持原值
  - [x] SubTask 5.3: 键名匹配改为大小写不敏感（将键名 `.toLowerCase()` 后与列表比较）
  - [x] SubTask 5.4: `redactFields` 为空数组或 falsy 时回退使用 `_DEFAULT_REDACT_FIELDS`

- [x] Task 6: tests/unit/test_redaction.py — 新增 exception_hook 路径脱敏用例
  - [x] SubTask 6.1: 新增测试 `test_capture_exception_message_redacted`：构造消息含 `password="xxx"` 的异常，调用 `capture_exception`，验证返回 dict 的 `message` 字段已被掩码
  - [x] SubTask 6.2: 新增测试 `test_capture_exception_traceback_redacted`：构造 traceback 含敏感信息的异常，验证 `traceback` 字段已被掩码
  - [x] SubTask 6.3: 新增测试 `test_format_trace_for_ai_redacted`：构造含敏感信息的 exc_data，调用 `format_trace_for_ai`，验证返回文本已被掩码

# Task Dependencies
- Task 3 (redaction.py warning) 无依赖，可独立进行
- Task 4 (config.py 注释) 无依赖，可独立进行
- Task 5 (ai-debug.js SDK) 无依赖，可独立进行
- Task 1 (exception_hook.py) 依赖已有 redact() 函数，无新依赖
- Task 2 (stacktrace.py) 依赖已有 redact() 函数，无新依赖
- Task 6 (测试) 依赖 Task 1 和 Task 2 完成后编写

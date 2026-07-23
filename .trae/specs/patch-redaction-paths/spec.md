# 修补 exception_hook 脱敏路径，锁定 redaction_enabled，SDK body 深度脱敏 Spec

## Why
当前 `capture_exception` 返回的 `message`（异常消息）和 `traceback`（完整堆栈文本）未经 `redact()` 处理，可能泄露密码、token 等敏感信息。`format_trace_for_ai` 输出的文本同样缺少脱敏。SDK 端 `_redact` 仅做键名精确匹配，对字符串类型的请求体（如 JSON string body）无法深度脱敏，且敏感键名列表不完整，前后端不一致。

## What Changes
- **exception_hook.py**: `capture_exception` 后对返回 dict 中的 `message` 和 `traceback` 字段调用 `redact()`
- **stacktrace.py**: `format_trace_for_ai` 对最终输出文本调用 `redact()`
- **redaction.py**: `redaction_enabled=False` 时增加 `logger.warning` 告警（仅首次或每次调用均告警）
- **config.py**: 注释强调生产环境必须 `redaction_enabled=True`
- **ai-debug.js**: `_redact` 改为递归深度脱敏——对字符串值尝试 `JSON.parse` → 递归脱敏 → `JSON.stringify`
- **ai-debug.js**: 扩充敏感键名列表（大小写不敏感匹配，增加 cookie/access_token 等）
- **ai-debug.js**: `redactFields` 为空时默认使用内置列表，不关闭脱敏
- **test_redaction.py**: 新增 exception_hook 路径脱敏用例

## Impact
- Affected specs: redaction（脱敏模块）
- Affected code:
  - `app/mcp/hooks/exception_hook.py`
  - `app/mcp/collectors/stacktrace.py`
  - `app/mcp/core/redaction.py`
  - `app/config.py`
  - `browser-sdk/ai-debug.js`
  - `tests/unit/test_redaction.py`

## MODIFIED Requirements

### Requirement: exception_hook 脱敏补漏
`exception_hook.py` 中 `_hook` 和 `_asyncio_handler` 在调用 `capture_exception` 得到 exc_data 后，须对 `exc_data["message"]` 和 `exc_data["traceback"]` 调用 `redact()` 再传给 `record_error`。

#### Scenario: 异常消息含敏感信息
- **WHEN** 异常消息包含 `password="secret123"` 等敏感文本
- **THEN** 存入的 `message` 字段中该敏感值已被掩码为 `***`

#### Scenario: traceback 含敏感信息
- **WHEN** traceback 文本中包含 token/api_key 等敏感值
- **THEN** 存入的 `traceback` 字段中该敏感值已被掩码

### Requirement: format_trace_for_ai 脱敏
`format_trace_for_ai` 返回的纯文本须经过 `redact()` 处理后再返回。

#### Scenario: AI 格式化文本含敏感信息
- **WHEN** exc_data 中含有敏感字段（如 message 含 password）
- **THEN** `format_trace_for_ai` 返回的文本中敏感值已被掩码

### Requirement: redaction_enabled=False 告警
`redact()` 在 `settings.redaction_enabled=False` 时须输出 `logger.warning`，提醒管理员脱敏已关闭。

#### Scenario: 脱敏关闭时告警
- **WHEN** `redaction_enabled=False` 且 `redact()` 被调用
- **THEN** logger 输出 warning 级别日志，提示脱敏已禁用

### Requirement: config.py 生产环境注释
`redaction_enabled` 配置项注释须强调生产环境必须设为 `True`。

#### Scenario: 开发者查看配置
- **WHEN** 开发者查看 config.py 中 `redaction_enabled` 定义
- **THEN** 注释明确说明生产环境禁止设为 False

### Requirement: SDK _redact 深度脱敏
`_redact` 对字符串值尝试 `JSON.parse`，若成功则递归脱敏后 `JSON.stringify` 回写；失败则保持原值。

#### Scenario: 请求体为 JSON 字符串
- **WHEN** 网络请求 body 为 `'{"password":"123"}'` 字符串
- **THEN** 上报的 body 中 password 值被替换为 `"***REDACTED***"`

#### Scenario: 普通字符串不被破坏
- **WHEN** 值为普通字符串（非 JSON）
- **THEN** 原样保留，不被修改

### Requirement: SDK 敏感键名列表扩充
SDK 内置敏感键名列表须扩充至覆盖 cookie、access_token、api_key、passwd、pwd 等，且匹配时大小写不敏感。

#### Scenario: 大小写不敏感匹配
- **WHEN** 对象含键 `Password` 或 `TOKEN`
- **THEN** 值被替换为 `"***REDACTED***"`

#### Scenario: 新增敏感键覆盖
- **WHEN** 对象含键 `cookie`、`access_token`、`api_key`
- **THEN** 值被替换为 `"***REDACTED***"`

### Requirement: SDK redactFields 空值回退
当 `cfg.redactFields` 为空数组或 falsy 时，`_redact` 使用内置默认列表，而非跳过脱敏。

#### Scenario: redactFields 被设为空数组
- **WHEN** 用户 `init({ redactFields: [] })`
- **THEN** `_redact` 仍使用内置默认敏感列表进行脱敏

### Requirement: 新增 exception_hook 路径脱敏测试
`tests/unit/test_redaction.py` 新增测试用例，验证 `capture_exception` 返回的 message/traceback 经过脱敏处理。

#### Scenario: 异常消息脱敏验证
- **WHEN** 构造一个消息含 `password="xxx"` 的异常并调用 `capture_exception`
- **THEN** 返回 dict 的 `message` 字段中 password 值已被掩码

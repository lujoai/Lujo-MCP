# 为 errors._recent 增加 session_id 维度 Spec

## Why
当前 `errors._recent` 是一个全局 deque，所有会话的异常混在一起。MCP 工具层（`list_recent_traces`/`search_logs`）无法按会话过滤，导致多会话场景下 AI 拿到的错误数据互相干扰，无法定位到特定会话的问题。

## What Changes
- `errors.py`: `_recent` 从 `deque` 改为 `dict[session_id, deque]`；`record()`/`list_recent()`/`search()`/`get_latest()`/`get_by_id()` 增加 `session_id` 参数
- `trace_api.py`: `list_recent_traces()`/`search_logs()` 增加 `session_id` 参数，透传给 errors 层过滤
- `trace_repo.py`: `save_trace()` 增加 `session_id` 参数，写入时关联会话
- `ingest.py`: 各 ingest 端点从请求体提取 `session_id` 传入 `save_trace()`
- 新增 `tests/unit/test_errors.py`：会话隔离单元测试

## Impact
- Affected specs: errors 内存缓冲、trace 工具查询、ingest 接入层
- Affected code:
  - `app/mcp/core/errors.py` — 核心数据结构变更
  - `app/mcp/core/trace_repo.py` — save_trace 签名变更
  - `app/mcp/tools/trace_api.py` — 查询工具签名变更
  - `app/api/ingest.py` — HTTP 端点提取 session_id
  - `tests/unit/test_errors.py` — 新增测试文件

## MODIFIED Requirements

### Requirement: errors 内存缓冲按会话隔离
`_recent` SHALL 改为 `dict[str, deque]`，key 为 `session_id`。
- **写入时**：未提供 `session_id` 使用 `"_global"` 作为默认 key，保持向后兼容。
- **读取时**：`session_id=None` 表示聚合所有桶（全部会话），保证 dashboard 等现有调用方零改动仍能看到全量数据。

#### Scenario: record 带 session_id
- **WHEN** 调用 `record(exc_data, source, session_id="sess-abc")`
- **THEN** 异常记录存入 `_recent["sess-abc"]`，不影响其他 session 的 deque

#### Scenario: record 不带 session_id
- **WHEN** 调用 `record(exc_data, source)` （无 session_id）
- **THEN** 异常记录存入 `_recent["_global"]`，行为与改造前一致

#### Scenario: list_recent 按 session_id 过滤
- **WHEN** 调用 `list_recent(limit=10, session_id="sess-abc")`
- **THEN** 仅返回 `_recent["sess-abc"]` 中的记录

#### Scenario: list_recent 不传 session_id 聚合全部
- **WHEN** 调用 `list_recent(limit=10)` （session_id=None）
- **THEN** 聚合所有桶的记录，按 last_seen 降序返回前 limit 条

#### Scenario: search 按 session_id 过滤
- **WHEN** 调用 `search(keyword, since_minutes=30, session_id="sess-abc")`
- **THEN** 仅在 `_recent["sess-abc"]` 中搜索

#### Scenario: search 不传 session_id 搜索全部
- **WHEN** 调用 `search(keyword, since_minutes=30)` （session_id=None）
- **THEN** 在所有桶中搜索

#### Scenario: get_latest 按 session_id 过滤
- **WHEN** 调用 `get_latest(session_id="sess-abc")`
- **THEN** 仅返回该会话下 `last_seen` 最大的一条

#### Scenario: get_by_id 按 session_id 过滤
- **WHEN** 调用 `get_by_id(error_id, session_id="sess-abc")`
- **THEN** 仅在该会话的 deque 中查找；session_id=None 时在全局查找

### Requirement: trace_repo.save_trace 绑定 session_id
`save_trace()` SHALL 接受可选 `session_id` 参数，传递给 `_record_error()`。

#### Scenario: save_trace 带 session_id
- **WHEN** 调用 `save_trace(..., session_id="sess-abc")`
- **THEN** 异常同时写入 errors 缓冲的 `"sess-abc"` 桶和 trace_store

### Requirement: trace_api 工具层透传 session_id
`list_recent_traces()` 和 `search_logs()` SHALL 接受可选 `session_id` 参数，透传给 errors 层方法。

#### Scenario: list_recent_traces 按会话过滤
- **WHEN** 调用 `list_recent_traces(limit=10, session_id="sess-abc")`
- **THEN** 内存异常部分仅返回该会话的记录

#### Scenario: search_logs 按会话过滤
- **WHEN** 调用 `search_logs(keyword, since_minutes=30, session_id="sess-abc")`
- **THEN** 内存异常部分仅在该会话中搜索

### Requirement: ingest 端点提取 session_id
各 ingest 端点 SHALL 从请求体中提取 `session_id` 并传入对应的处理函数。

#### Scenario: ingest/error 带 session_id
- **WHEN** POST `/ingest/error` 请求体包含 `{"session_id": "sess-abc", ...}`
- **THEN** `session_id` 被提取并传入 `save_trace()`

## REMOVED Requirements
无

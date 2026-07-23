# Tasks

- [x] Task 1: 改造 errors.py — _recent 按 session_id 分桶
  - [x] SubTask 1.1: 将 `_recent: deque` 改为 `_recent: dict[str, deque] = {}`，新增辅助函数 `_get_bucket(session_id)` 按需创建 deque
  - [x] SubTask 1.2: `record()` 增加 `session_id: str | None = None` 参数，写入时用 `session_id or "_global"` 定位桶，指纹去重仅在桶内进行
  - [x] SubTask 1.3: `list_recent()` 增加 `session_id: str | None = None` 参数；session_id 非 None 时只读对应桶，None 时聚合所有桶
  - [x] SubTask 1.4: `search()` 增加 `session_id: str | None = None` 参数；逻辑同 list_recent
  - [x] SubTask 1.5: `get_latest()` 增加 `session_id: str | None = None` 参数；None 时聚合所有桶
  - [x] SubTask 1.6: `get_by_id()` 增加 `session_id: str | None = None` 参数；None 时遍历所有桶

- [x] Task 2: 改造 trace_repo.py — save_trace 绑定 session_id
  - [x] SubTask 2.1: `save_trace()` 增加 `session_id: str | None = None` 参数，透传给 `_record_error()`
  - [x] SubTask 2.2: `get_trace()` 增加 `session_id: str | None = None` 参数，透传给 `_get_latest()` / `_get_error()`

- [x] Task 3: 改造 trace_api.py — 工具层透传 session_id
  - [x] SubTask 3.1: `list_recent_traces()` 增加 `session_id: str | None = None` 参数，传给 `list_recent()`
  - [x] SubTask 3.2: `search_logs()` 增加 `session_id: str | None = None` 参数，传给 `search_errors()`

- [x] Task 4: 改造工具层入口 — ingest_api / silent_failure_api / ingest.py 透传 session_id
  - [x] SubTask 4.1: `tool_ingest_error()` 增加 `session_id` 参数，传给 `save_trace()`
  - [x] SubTask 4.2: `tool_ingest_silent_failure()` 增加 `session_id` 参数，传给 `save_trace()`
  - [x] SubTask 4.3: `ingest.py` 各端点从 `req.get("session_id")` 提取并传入对应工具函数

- [x] Task 5: 更新 conftest.py 适配新数据结构
  - [x] SubTask 5.1: `_isolate_errors_store` fixture 中 `errors._recent.clear()` 已兼容 dict（无需修改）

- [x] Task 6: 补充测试 — tests/unit/test_errors.py
  - [x] SubTask 6.1: 测试 record 带 session_id 写入对应桶，不影响其他桶
  - [x] SubTask 6.2: 测试 record 不带 session_id 写入 `_global` 桶
  - [x] SubTask 6.3: 测试 list_recent 按 session_id 过滤 + 不传 session_id 聚合全部
  - [x] SubTask 6.4: 测试 search 按 session_id 过滤 + 不传 session_id 搜索全部
  - [x] SubTask 6.5: 测试 get_by_id 按 session_id 过滤
  - [x] SubTask 6.6: 测试指纹去重仅在桶内生效（同指纹在不同桶各自独立）

- [x] Task 7: 运行测试验证
  - [x] SubTask 7.1: 运行 `pytest tests/unit/test_errors.py` 确认新增用例全部通过（7/7 passed）
  - [x] SubTask 7.2: 运行 `pytest tests/unit/` 确认现有测试无回归（266 passed, 1 pre-existing failure in test_git.py unrelated）

# Task Dependencies
- Task 2 依赖 Task 1（save_trace 调用 _record_error）
- Task 3 依赖 Task 1（trace_api 调用 errors 函数）
- Task 4 依赖 Task 2（ingest 工具调用 save_trace）
- Task 5 依赖 Task 1（conftest 直接访问 _recent）
- Task 6 依赖 Task 1（测试直接调用 errors 函数）
- Task 7 依赖 Task 1-6 全部完成

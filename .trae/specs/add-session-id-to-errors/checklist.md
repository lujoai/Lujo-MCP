# Checklist

- [x] `errors._recent` 类型为 `dict[str, deque]`，不再是非 dict 类型
- [x] `errors.record()` 接受 `session_id` 参数，写入对应桶；不传时写入 `_global`
- [x] `errors.list_recent()` 接受 `session_id` 参数，按桶过滤；None 时聚合全部
- [x] `errors.search()` 接受 `session_id` 参数，按桶过滤；None 时搜索全部
- [x] `errors.get_latest()` 接受 `session_id` 参数，按桶过滤；None 时聚合全部
- [x] `errors.get_by_id()` 接受 `session_id` 参数，按桶过滤；None 时遍历全部
- [x] 指纹去重仅在同一个 session_id 桶内生效，不同桶互不影响
- [x] `trace_repo.save_trace()` 接受 `session_id` 参数并透传给 `errors.record()`
- [x] `trace_repo.get_trace()` 接受 `session_id` 参数并透传给 errors 查询函数
- [x] `trace_api.list_recent_traces()` 接受 `session_id` 参数并透传给 `errors.list_recent()`
- [x] `trace_api.search_logs()` 接受 `session_id` 参数并透传给 `errors.search()`
- [x] `ingest_api.tool_ingest_error()` 接受 `session_id` 参数并透传给 `save_trace()`
- [x] `silent_failure_api.tool_ingest_silent_failure()` 接受 `session_id` 参数并透传给 `save_trace()`
- [x] `ingest.py` 各 POST 端点从请求体提取 `session_id` 并传入工具函数
- [x] `conftest.py` 的 `_isolate_errors_store` fixture 兼容新的 dict 数据结构
- [x] `tests/unit/test_errors.py` 存在且覆盖会话隔离、聚合查询、指纹去重桶内隔离等场景
- [x] 现有 `tests/unit/` 全部测试通过，无回归（1 个 test_git.py 预存失败与本次无关）

# Checklist

> 验证时逐项检查代码与测试，满足即在行首打 `[x]`。任一项失败须在 `tasks.md` 新增修复任务并重验。

## SEC-13 spec_store 原子性
- [x] `spec_store.update()` 不再调用 `delete_logs`，改为先 `add_log` 写新版本（crash-safe append）
- [x] `update()` 内最终 `add_log` 不再裸露在 try/except 之外或不再有"二次写"逻辑
- [x] `get()` 存储回读分支按 `updated_at`（回退 `timestamp`）取最新条目，而非首条
- [x] `_do_restore()` 同 spec_id 多条时取 `updated_at` 最大者
- [x] 新增测试 `test_update_appends_new_version_without_delete` 通过
- [x] 新增测试 `test_read_returns_newest_when_multiple_versions` 通过
- [x] 新增测试 `test_restore_picks_newest_version` 通过
- [x] 既有 spec_store 用例（TestCreateAndGet / TestUpdate / TestDelete / TestListSpecs / TestRestoreFromStorage）全部通过

## SEC-13 trace_repo 原子性
- [x] `save_trace()` 写入顺序为 `_STEP_META` →（条件）`_STEP_LINK` → `_STEP_DATA`（commit-marker）
- [x] `_record_error` 调用仍在写存储之前，位置未变
- [x] 读取路径（`_rebuild_trace_from_store` / `get_trace`）未被改动或改动后语义一致
- [x] 新增测试 `test_save_trace_writes_data_last_as_commit_marker` 通过
- [x] 新增测试 `test_save_trace_data_present_implies_meta_present` 通过
- [x] 既有 trace_repo 用例全部通过

## M7 API_KEY 鉴权语义
- [x] `config.py` `model_post_init` 将空串/纯空白 `api_key` 归一化为 `None`
- [x] 归一化时记录 warning 日志
- [x] 既有"未知 .env 键 warning"逻辑未被破坏
- [x] `middleware.py` 未被修改（`AuthMiddleware` 仍用 `enabled = api_key is not None` + `hmac.compare_digest`）
- [x] 新增测试 `test_empty_api_key_normalized_to_none` 通过
- [x] 新增测试 `test_whitespace_api_key_normalized_to_none` 通过
- [x] 新增测试 `test_nonempty_api_key_preserved` 通过

## AI_RULES 合规
- [x] 未修改 `app/mcp/core/storage/pg_store.py`
- [x] 未修改 `storage/base.py` / `memory_store.py`（未引入事务接口）
- [x] 未绕过 Storage（仍通过 `add_log` / `get_logs` / `delete_logs`）
- [x] 未修改 fail-closed 鉴权比较逻辑（`hmac.compare_digest` 不变）
- [x] 遵循最小修改原则，无无关改动

## 回归
- [x] `pytest tests/unit/test_spec_store.py tests/unit/test_trace_repo.py tests/unit/test_config.py tests/unit/test_middleware.py` 全部通过（57 passed）
- [x] 全量 `pytest` 基线不低于现状（292 passed > 284，1 pre-existing failure in test_git.py，6 skipped）

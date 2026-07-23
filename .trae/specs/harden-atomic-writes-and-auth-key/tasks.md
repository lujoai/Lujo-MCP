# Tasks

> 三个 Task 文件无重叠，可由多个子智能体并行执行。
> 每个 Task 须遵守 `docs/internal/AI_RULES.md`：最小修改、不绕过 Storage、不修改 fail-closed 鉴权比较逻辑、不修改 `pg_store.py`。
> 本任务不涉及 `pg_store.py`，无需 PGStore 审批门。

- [x] Task 1: SEC-13 修复 spec_store 非原子写入（`app/mcp/verifier/spec_store.py` + `tests/unit/test_spec_store.py`）
  - [x] 1.1 重构 `update()` 为 crash-safe append：先 `add_log(spec_id, _STEP_SPEC, updated)` 写入新版本（提交点），移除"先写后删再写"逻辑；`update()` 内不再调用 `delete_logs`（删除仅由 `delete()` 负责）。保留内存层 `_specs` 更新与锁逻辑不变。
  - [x] 1.2 修复读取取最新版本：`get()` 存储回读分支（当前 ~121-127 行取首条）改为按 `updated_at`（回退 entry `timestamp`）取**最新**条目；`_do_restore()`（~42-56 行取首条未重复）改为同 spec_id 多条时取 `updated_at` 最大者。
  - [x] 1.3 补充测试（`test_spec_store.py`，复用既有 `_isolate_spec_store` fixture）：
    - `test_update_appends_new_version_without_delete`：update 后存储层存在新版本条目，且未触发 delete。
    - `test_read_returns_newest_when_multiple_versions`：手动向存储层注入旧+新两条 `step="spec"` 条目，`get()` 回读返回 `updated_at` 最大者。
    - `test_restore_picks_newest_version`：`_do_restore()` 在多条同 id 时取最新。
- [x] Task 2: SEC-13 修复 trace_repo save_trace 非原子写入（`app/mcp/core/trace_repo.py` + `tests/unit/test_trace_repo.py`）
  - [x] 2.1 调整 `save_trace()` 写入顺序为 commit-marker：先写 `trace_meta`（`_STEP_META`），再写条件 `trace_link`（`_STEP_LINK`，仅当 `trace_id` 提供），最后写 `trace_data`（`_STEP_DATA`）作为提交标记。各 `add_log` 的 try/except 与日志保持不变；`_record_error` 调用位置不变（仍在写存储之前）。
  - [x] 2.2 验证读取路径无需改动：`_rebuild_trace_from_store` / `get_trace` 已按 step 扫描 + 可选字段容错，确认调整顺序后既有用例仍通过。
  - [x] 2.3 补充测试（`test_trace_repo.py`）：
    - `test_save_trace_writes_data_last_as_commit_marker`：mock `add_log`，断言调用顺序为 meta(→link)→data。
    - `test_save_trace_data_present_implies_meta_present`：save_trace 成功后，`get_logs(error_id)` 中 `trace_data` 与 `trace_meta` 同时存在。
- [x] Task 3: M7 修复 API_KEY 空串鉴权语义（`app/config.py` + `tests/unit/test_config.py`）
  - [x] 3.1 在 `Settings.model_post_init` 中归一化：`if self.api_key is not None and not self.api_key.strip(): self.api_key = None` 并 `logger.warning("API_KEY 为空，已视为未配置，鉴权关闭")`。注意保留既有"未知 .env 键 warning"逻辑。
  - [x] 3.2 确认 `app/middleware.py` 无需修改：`AuthMiddleware.enabled = self.api_key is not None` 在归一化后对空串正确为 `False`；非空 key 时 `hmac.compare_digest` 保持 fail-closed。
  - [x] 3.3 补充测试（`test_config.py`，参照既有用例的 `_TestSettings` 模式或直接测 `app.config.settings`）：
    - `test_empty_api_key_normalized_to_none`：`API_KEY=""` → `settings.api_key is None` + warning 被记录。
    - `test_whitespace_api_key_normalized_to_none`：`API_KEY="   "` → `settings.api_key is None`。
    - `test_nonempty_api_key_preserved`：`API_KEY="secret"` → `settings.api_key == "secret"`，无归一化 warning。

# Task Dependencies
- Task 1、Task 2、Task 3 互不依赖（文件无重叠），可三个子智能体并行执行。
- Task 1 与 Task 2 同属 SEC-13 但改不同文件（`spec_store.py` vs `trace_repo.py`），可独立并行。

# 方向 A：SEC-13 非原子写入 + M7 API_KEY 空串鉴权 Spec

> 对应 `docs/internal/release/claude-audit-consolidated.md`「下一步执行计划 · 第一阶段」。
> 范围：SEC-13（非原子写入）+ M7（空串 `API_KEY` 使鉴权"开而无锁"）。
> 执行方式：多个子智能体并行（文件无重叠）。

---

## Why

1. **SEC-13**：`spec_store.update()` 采用"先写后删再写"的非原子模式，进程在 `delete_logs` 与最终 `add_log` 之间崩溃会导致 spec 在持久层丢失（重启后 `_restore_if_needed()` 找不到该 spec）；且各读取路径取**首条**而非**最新**条目，旧版本与新版本共存时会读到陈旧数据。`trace_repo.save_trace()` 顺序写入 3 条 `add_log`（DATA→META→LINK），非原子，崩溃可产生半成品 trace。
2. **M7**：`config.py` 中 `api_key: Optional[str] = None`，当 `.env` 设置 `API_KEY=""`（空串）时，`AuthMiddleware.enabled = ("" is not None) = True`，鉴权"开启"，但 `hmac.compare_digest("", "")` 恒为 `True` —— 任何不带 key 的请求都能通过，形成"开而无锁"的鉴权假象，违背"不设置 = 不鉴权"的既定语义。

存储抽象层（`storage/base.py`）仅提供 `save_entry`（追加）/ `get_entries` / `delete`（按 key 全删），**无事务接口**，因此原子性须通过"写入顺序 + 幂等读取最新"实现，不得引入新存储后端或事务框架（遵守 AI_RULES）。

## What Changes

### SEC-13 非原子写入
- **`app/mcp/verifier/spec_store.py`**：
  - 重构 `update()`：改为 **crash-safe append** —— 先 `add_log` 写入新版本（持久化提交点），移除"先写后删再写"模式；旧版本交由读取端取最新 + 存储 TTL 清理，更新过程中不再做删除（删除只保留在 `delete()` 显式调用）。
  - 修复读取路径取最新版本：`get()` 的存储回读分支与 `_do_restore()` 均按 `updated_at`（回退 `timestamp`）取**最新**条目，而非首条。
- **`app/mcp/core/trace_repo.py`**：
  - 调整 `save_trace()` 写入顺序为 **commit-marker 模式**：先写 `trace_meta`（与条件 `trace_link`），最后写 `trace_data` 作为提交标记。`trace_data` 存在即保证其前的元数据已落库；崩溃在 `trace_data` 之前则无 trace 记录（干净失败，`_rebuild_trace_from_store()` 返回 `None`）。
  - 读取路径（`_rebuild_trace_from_store` / `get_trace`）**不变**（已按 step 容错可选字段）。

### M7 API_KEY 空串鉴权语义收口
- **`app/config.py`**：
  - 在 `model_post_init` 中归一化：当 `api_key` 为空串或纯空白时，置为 `None` 并 `logger.warning` 提示"空 API_KEY 已视为未配置，鉴权关闭"。使"空 = 未配置 = 不鉴权"语义集中收口于配置层。
  - `AuthMiddleware`（`app/middleware.py`）**不修改**：`enabled = self.api_key is not None` 在归一化后自然为 `False`；非空 key 时 `hmac.compare_digest` 保持 fail-closed 不变。

### 不变更（AI_RULES 约束）
- ❌ 不修改 `app/mcp/core/storage/pg_store.py`（无需审批门）。
- ❌ 不修改 `storage/base.py` / `memory_store.py`（不引入事务接口）。
- ❌ 不绕过 Storage、不新建数据库连接、不修改 fail-closed 比较逻辑。

## Impact

- **Affected specs**：`claude-audit-consolidated.md`（SEC-13、M7 状态将由待处理 → 已修复）。
- **Affected code**：
  - `app/mcp/verifier/spec_store.py`（`update` / `get` / `_do_restore`）
  - `app/mcp/core/trace_repo.py`（`save_trace` 写入顺序）
  - `app/config.py`（`model_post_init` 归一化）
  - `tests/unit/test_spec_store.py`、`tests/unit/test_trace_repo.py`、`tests/unit/test_config.py`（补充测试）
- **行为兼容性**：
  - spec 读取语义从"首条"变为"最新条"——对既有调用方透明（单版本场景无差异）。
  - `API_KEY=""` 的既有部署将从"假鉴权"变为"显式不鉴权 + 警告"——这是修正而非破坏，符合文档既定语义。
- **风险**：低。所有改动在既有存储原语之上，无 schema 变更，无新依赖。

---

## MODIFIED Requirements

### Requirement: Spec Store 写入原子性（SEC-13）
`spec_store.update()` SHALL 以 crash-safe 方式持久化新版本：新版本 SHALL 在任何旧版本被移除之前写入存储层。当同一 `spec_id` 下存在多个 `step="spec"` 条目时，所有读取路径（`get` 存储回读分支、`_do_restore`）SHALL 返回 `updated_at` 最大（回退 `timestamp` 最大）的条目。`update()` 过程中 SHALL NOT 执行 `delete_logs`（删除仅由显式 `delete()` 负责）。

#### Scenario: 更新后崩溃不丢数据
- **WHEN** `update(spec_id, patch)` 写入新版本后、进程崩溃
- **THEN** 重启后 `get(spec_id)` / `list_specs()` 仍能从存储层读到最新版本（旧版本可能共存，但读取取最新）

#### Scenario: 多版本共存读取最新
- **GIVEN** 存储层同一 `spec_id` 下存在旧版本与新版本两条 `step="spec"` 条目
- **WHEN** 调用 `get(spec_id)` 触发存储回读
- **THEN** 返回 `updated_at` 最大的那条，而非首条

### Requirement: Trace 写入原子性（SEC-13，commit-marker）
`trace_repo.save_trace()` SHALL 先写入 `trace_meta`（及条件 `trace_link`），最后写入 `trace_data` 作为提交标记。`trace_data` 在存储层存在 SHALL 保证其前的元数据已落库。崩溃发生在 `trace_data` 写入之前 SHALL 不留下半成品 trace（`_rebuild_trace_from_store()` 返回 `None`）。

#### Scenario: 提交标记保证一致性
- **WHEN** `save_trace()` 完成 `trace_data` 写入
- **THEN** `trace_meta`（及若提供 `trace_id` 时的 `trace_link`）必然已落库，`_rebuild_trace_from_store()` 可完整重建

#### Scenario: 崩溃在提交标记前干净失败
- **WHEN** `save_trace()` 在写 `trace_data` 之前崩溃
- **THEN** 存储层无 `trace_data` 条目，`_rebuild_trace_from_store()` 返回 `None`（不返回半成品）

### Requirement: API_KEY 鉴权语义收口（M7）
配置层 SHALL 将空串或纯空白 `api_key` 归一化为 `None`（视为"未配置"），并记录 warning 日志。`api_key` 为 `None` 时鉴权 SHALL 关闭；`api_key` 为非空字符串时鉴权 SHALL 保持 fail-closed（`hmac.compare_digest` 恒定时间比较，不变）。

#### Scenario: 空串 API_KEY 视为未配置
- **GIVEN** `.env` 设置 `API_KEY=""`（或纯空白）
- **WHEN** `Settings()` 初始化
- **THEN** `settings.api_key is None`，启动日志含 warning；`AuthMiddleware.enabled is False`

#### Scenario: 非空 API_KEY 保持 fail-closed
- **GIVEN** `API_KEY="real-secret"`
- **WHEN** 请求未带正确 key
- **THEN** 返回 401（`hmac.compare_digest` fail-closed 不变）

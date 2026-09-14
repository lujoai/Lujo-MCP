# PostgreSQL Migrations 归档（已失效）

> 归档日期：2026-09-14；来源工作包：Step 3 **WP6**（`chore(storage): remove PostgreSQL deployment dependencies`）。
> 归档方式：`git mv`（保留完整 Git 历史，可追溯至 `migrations/` 时期的原始提交）。
> 落点说明：设计文档 §7.1 原定 `docs/internal/archive/pg-migrations/`，因该目录被
> `.gitignore`（`docs/internal/`）忽略、无法被 Git 管理，按 §7.4-M6 的作者备选方案
> 改为仓库根 `archive/pg-migrations/`（被 Git 跟踪）。

## 这些 DDL 已失效

v0.9.0 起（Step 3）正式移除 PostgreSQL 后端后，**新版本不再创建、不再读取、不再迁移
这些表**。唯一的例外是 `kb_entries`：其数据曾可经一次性迁移脚本（v0.8.x 的
`scripts/migrate_pg_kb_to_sqlite.py`，已随 WP5 删除）迁到本地 SQLite「笔记本」；
SQLite 版本的表结构独立定义在 `app/runtime/core/storage/sqlite_kb_store.py`，
与本目录 SQL 无关。

仍在使用 v0.8.x 及更早版本并显式配置 `STORAGE_BACKEND=postgresql` 的用户，其库表
结构以本归档为准；traces / errors / sessions / specs **没有迁移路径**，随 PG 后端
移除回到 memory-only（重启即清）。

## 文件清单（表名与用途）

| 文件 | 表 | 用途 |
|---|---|---|
| `20260710_create_sessions_table.sql` | `sessions` | 会话记录 |
| `20260710_create_traces_table.sql` | `traces` | 运行现场 trace 主表 |
| `20260711_create_errors_table.sql` | `errors` | 错误聚合（含 occurrence 计数） |
| `20260711_create_specs_table.sql` | `specs` | API 规范存储 |
| `20260817_create_kb_entries_table.sql` | `kb_entries` | RAG 知识库经验（曾有迁移路径，见上） |
| `20260827_create_traces_archive_table.sql` | `traces_archive` | 过期 traces 归档表 |

## 相关参考

- 迁移工具的设计与边界：`docs/internal/DESIGN_STEP3_PG_REMOVAL_20260913.md` §4（内部文档，不入版本库）
- 历史 PG 排障与迁移说明：[TROUBLESHOOTING.md](../../docs/public/TROUBLESHOOTING.md) L 节

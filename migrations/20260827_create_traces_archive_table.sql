-- traces 归档表（Phase 5 P3-2）：冷数据归档，结构与 traces 主表对齐 + archived_at 归档时间列。
-- 本文件与 app/runtime/core/storage/ddl.py 的 DDL_TRACES_ARCHIVE 保持一致。
-- FIX: P2-D4 —— 此前 ddl.py 定义了 DDL_TRACES_ARCHIVE 但 migrations/ 无对应迁移文件，
-- 纯迁移方式部署开启 pg_archive_enabled 后归档会静默失败。本文件补齐缺口。
-- 新表用 CREATE TABLE IF NOT EXISTS，天然幂等；无旧 schema 兼容段（历史无 archive 表）。

CREATE TABLE IF NOT EXISTS traces_archive (
    id          BIGINT,
    request_id  TEXT        NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL,
    step        TEXT        NOT NULL,
    data        JSONB,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_traces_archive_rid ON traces_archive(request_id);
CREATE INDEX IF NOT EXISTS idx_traces_archive_ts  ON traces_archive(timestamp);
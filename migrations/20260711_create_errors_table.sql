-- errors 表（异常聚合）：本文件与 app/runtime/core/storage/ddl.py 的 DDL_ERRORS 保持一致。
-- 历史背景（P0-5）：旧迁移使用 trace_id/stack/file/line 列，与代码（error_id/frames/frame_count/
-- traceback/source/session_id + 唯一索引 uq_errors_fp_session）完全不一致，导致 PG 后端 errors/specs
-- 静默失效。2026-08 起以代码 DDL 为准重建；下方兼容段对已按旧 schema 建库的环境补齐缺失列。

CREATE TABLE IF NOT EXISTS errors (
    id                  BIGSERIAL PRIMARY KEY,
    error_id            TEXT,
    fingerprint         TEXT,
    exception_type      TEXT,
    message             TEXT,
    frames              JSONB,
    frame_count         INTEGER DEFAULT 0,
    traceback           TEXT,
    source              TEXT,
    session_id          TEXT,
    occurrence_count    INTEGER DEFAULT 1,
    first_seen          DOUBLE PRECISION,
    last_seen           DOUBLE PRECISION,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── 旧 schema 兼容（幂等）──
-- 已按旧迁移建库的环境缺少 error_id/frames/frame_count/traceback/source/session_id/
-- first_seen/last_seen 列，逐列补齐；旧列 trace_id/stack/file/line 保留但代码不再读写。
-- FIX: P1-D3 —— 兼容 ALTER 段必须排在下方 CREATE INDEX **之前**：旧 schema 库无
-- session_id 列，先建索引会报 "column session_id does not exist" 使脚本中断，
-- 后续补列永远执行不到（旧库执行必失败）。全新建库时本段全部为幂等 no-op。
ALTER TABLE errors ADD COLUMN IF NOT EXISTS fingerprint TEXT;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS error_id TEXT;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS frames JSONB;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS frame_count INTEGER DEFAULT 0;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS traceback TEXT;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS first_seen DOUBLE PRECISION;
ALTER TABLE errors ADD COLUMN IF NOT EXISTS last_seen DOUBLE PRECISION;

-- 索引依赖的列已由上方 CREATE TABLE / 兼容 ALTER 保证存在
CREATE UNIQUE INDEX IF NOT EXISTS uq_errors_fp_session ON errors(fingerprint, session_id);
CREATE INDEX IF NOT EXISTS idx_errors_error_id ON errors(error_id);

-- 本表由 pg_store.py 的 upsert_error 方法使用（app/mcp/core/errors.py 记录异常聚合），
-- 与 traces 表 step 字段并存，用于异常指纹聚合/根因排序/历史查询。
CREATE TABLE IF NOT EXISTS errors (
    id                  BIGSERIAL PRIMARY KEY,
    trace_id            TEXT,
    exception_type      TEXT,
    message             TEXT,
    stack               TEXT,
    file                TEXT,
    line                INTEGER,
    fingerprint         TEXT,
    occurrence_count    INTEGER DEFAULT 1,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_errors_trace_id ON errors(trace_id);
CREATE INDEX IF NOT EXISTS idx_errors_fingerprint ON errors(fingerprint);
CREATE INDEX IF NOT EXISTS idx_errors_created_at ON errors(created_at);
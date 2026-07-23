-- DEPRECATED: app 改用 traces 表 step 字段存储，本表从未被代码使用，保留仅供历史参考
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
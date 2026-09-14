CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  DOUBLE PRECISION NOT NULL,
    last_active DOUBLE PRECISION NOT NULL,
    metadata    JSONB
);
CREATE INDEX IF NOT EXISTS idx_sessions_la ON sessions(last_active);
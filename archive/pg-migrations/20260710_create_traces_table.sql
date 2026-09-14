CREATE TABLE IF NOT EXISTS traces (
    id          BIGSERIAL PRIMARY KEY,
    request_id  TEXT        NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL,
    step        TEXT        NOT NULL,
    data        JSONB
);
CREATE INDEX IF NOT EXISTS idx_traces_rid ON traces(request_id);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(timestamp);
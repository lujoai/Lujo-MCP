-- DEPRECATED: app 改用 traces 表 step 字段存储，本表从未被代码使用，保留仅供历史参考
CREATE TABLE IF NOT EXISTS specs (
    id          TEXT PRIMARY KEY,
    kind        TEXT,
    target      TEXT,
    expect      JSONB,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_specs_kind ON specs(kind);
CREATE INDEX IF NOT EXISTS idx_specs_target ON specs(target);
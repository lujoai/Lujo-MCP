-- 本表由 pg_store.py 的 save_spec/get_spec/list_specs_pg/delete_spec 方法使用
-- （app/mcp/verifier/spec_store.py 规范存储），用于规范 CRUD 与进程重启后持久化。
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
-- specs 表（规范存储）：本文件与 app/runtime/core/storage/ddl.py 的 DDL_SPECS 保持一致。
-- 历史背景（P0-5）：旧迁移将 created_at/updated_at 定义为 TIMESTAMP DEFAULT CURRENT_TIMESTAMP，
-- 而代码（pg_store.py / async_pg_store.py）按 DOUBLE PRECISION（epoch 秒，time.time()）写入，
-- 类型不匹配导致 PG 后端规范写入静默失败。2026-08 起以代码 DDL 为准重建；
-- 下方兼容段对旧 schema 的 TIMESTAMP 列做幂等转换。

CREATE TABLE IF NOT EXISTS specs (
    id          TEXT PRIMARY KEY,
    kind        TEXT,
    target      TEXT,
    expect      JSONB,
    created_at  DOUBLE PRECISION,
    updated_at  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_specs_kind ON specs(kind);
CREATE INDEX IF NOT EXISTS idx_specs_target ON specs(target);

-- ── 旧 schema 兼容（幂等）──
-- 旧库 created_at/updated_at 为 TIMESTAMP，代码写入 epoch 浮点。仅在仍为
-- timestamp 类型时转换为 DOUBLE PRECISION（EXTRACT(EPOCH) 保留原值）。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'specs' AND column_name = 'created_at'
          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE specs ALTER COLUMN created_at TYPE DOUBLE PRECISION
            USING EXTRACT(EPOCH FROM created_at);
        ALTER TABLE specs ALTER COLUMN updated_at TYPE DOUBLE PRECISION
            USING EXTRACT(EPOCH FROM updated_at);
    END IF;
END $$;

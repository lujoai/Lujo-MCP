-- kb_entries 表（RAG 知识库持久化，v0.5.3）：本文件与 app/runtime/core/storage/ddl.py
-- 的 DDL_KB_ENTRIES 保持一致。KB 主存在进程内 KnowledgeBaseStore（OrderedDict），
-- 本表承担写穿持久化（upsert/record_verification/驱逐/clear 同步落库）与启动回灌
-- （最近 max_entries 条按 updated_at 倒序加载回内存），跨重启保留 learned 知识。

CREATE TABLE IF NOT EXISTS kb_entries (
    fingerprint            TEXT PRIMARY KEY,
    analysis               JSONB,
    fix_suggestion         TEXT,
    source                 TEXT,
    created_at             DOUBLE PRECISION NOT NULL,
    updated_at             DOUBLE PRECISION NOT NULL,
    normalized_fingerprint TEXT,
    type_fingerprint       TEXT,
    verify_count           INTEGER DEFAULT 0,
    case_confidence        DOUBLE PRECISION DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_entries_nfp ON kb_entries(normalized_fingerprint);
CREATE INDEX IF NOT EXISTS idx_kb_entries_tfp ON kb_entries(type_fingerprint);
CREATE INDEX IF NOT EXISTS idx_kb_entries_updated ON kb_entries(updated_at DESC);

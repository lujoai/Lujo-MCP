"""共享 PostgreSQL DDL 常量 —— P0-5 消除 pg_store / async_pg_store / migrations 三处分叉。

设计说明：
- 同步存储（pg_store.py，psycopg2）与异步存储（async_pg_store.py，asyncpg）必须使用
  同一份 DDL，避免两处手工维护导致表结构漂移。
- migrations/ 下的迁移文件也以本模块为唯一来源（脚本生成 SQL 或手工同步时对照本文件）。
- timestamp 语义：traces/sessions/specs 的 created_at/updated_at/last_active 均使用
  DOUBLE PRECISION（epoch 秒），由代码写入 ``time.time()``；errors 的 created_at/
  updated_at 使用 TIMESTAMP（数据库端生成），二者不可混用。
"""

# ── traces 主表 ──
DDL_TRACES = """
CREATE TABLE IF NOT EXISTS traces (
    id          BIGSERIAL PRIMARY KEY,
    request_id  TEXT        NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL,
    step        TEXT        NOT NULL,
    data        JSONB
);
CREATE INDEX IF NOT EXISTS idx_traces_rid ON traces(request_id);
CREATE INDEX IF NOT EXISTS idx_traces_ts  ON traces(timestamp);
"""

# ── sessions 会话表 ──
DDL_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  DOUBLE PRECISION NOT NULL,
    last_active DOUBLE PRECISION NOT NULL,
    metadata    JSONB
);
CREATE INDEX IF NOT EXISTS idx_sessions_la ON sessions(last_active);
"""

# ── errors 异常聚合表（Phase 2.3：按 fingerprint+session_id upsert，冲突时 occurrence_count+=1）──
DDL_ERRORS = """
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_errors_fp_session ON errors(fingerprint, session_id);
CREATE INDEX IF NOT EXISTS idx_errors_error_id ON errors(error_id);
"""

# ── specs 规范表（Phase 2.4：独立查询，消除 N+1 扫描）──
DDL_SPECS = """
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
"""

# ── kb_entries 知识库表（v0.5.3：RAG 知识库持久化，跨重启保留 learned 知识）──
# 时间戳语义与 traces/sessions/specs 一致：DOUBLE PRECISION（epoch 秒）。
DDL_KB_ENTRIES = """
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
"""

# ── traces 归档表（Phase 5 P3-2：结构同主表，用于冷数据归档）──
DDL_TRACES_ARCHIVE = """
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
"""

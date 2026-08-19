"""PG errors 存储实现 —— ErrorStorage ABC 的 PostgreSQL 后端（Phase 2.3）。

从 pg_store.py 拆出（god object 重构）：连接生命周期由 pg_executor 的
execute_sql 接管，不再内联 try/finally putconn 样板。
按 (fingerprint, session_id) upsert，冲突时 occurrence_count += 1。
"""

import json
import time
import logging

from app.runtime.core.storage.base import ErrorStorage
from app.runtime.core.storage.pg_executor import execute_sql, _ensure_init

logger = logging.getLogger("lujo-mcp.storage.pg")


class PGErrorStore(ErrorStorage):

    def upsert_error(self, record_data: dict) -> None:
        """upsert 一条错误记录到 errors 表。

        按 (fingerprint, session_id) 去重：
        - 不存在 → INSERT（occurrence_count=1）
        - 已存在 → occurrence_count += 1，刷新 last_seen/message/frames 等

        session_id 为 None 时写入 "_global"，与 errors 内存分桶逻辑一致。
        """
        _ensure_init()
        frames = record_data.get("frames")
        frames_json = (
            json.dumps(frames, ensure_ascii=False, default=str)
            if frames is not None
            else None
        )
        session_id = record_data.get("session_id") or "_global"
        now = record_data.get("last_seen") or time.time()
        execute_sql(
            """
            INSERT INTO errors
                (error_id, fingerprint, exception_type, message, frames,
                 frame_count, traceback, source, session_id,
                 occurrence_count, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint, session_id) DO UPDATE SET
                occurrence_count = errors.occurrence_count + 1,
                last_seen       = EXCLUDED.last_seen,
                updated_at      = CURRENT_TIMESTAMP,
                message         = EXCLUDED.message,
                frames          = EXCLUDED.frames,
                frame_count     = EXCLUDED.frame_count,
                traceback       = EXCLUDED.traceback,
                source          = EXCLUDED.source
            """,
            (
                record_data.get("error_id"),
                record_data.get("fingerprint"),
                record_data.get("type"),
                record_data.get("message"),
                frames_json,
                record_data.get("frame_count", 0),
                record_data.get("traceback"),
                record_data.get("source"),
                session_id,
                1,
                record_data.get("first_seen", now),
                now,
            ),
        )

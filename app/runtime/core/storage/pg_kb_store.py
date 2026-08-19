"""PG kb_entries 存储实现 —— KnowledgeBaseStorage ABC 的 PostgreSQL 后端（v0.5.3）。

从 pg_store.py 拆出（god object 重构）：RAG 知识库持久化。
KB 主存在进程内 KnowledgeBaseStore，本类承担写穿持久化 + 启动回灌。
连接生命周期由 pg_executor 的 execute_sql/query_sql 接管。
"""

import json
import time
import logging

from app.runtime.core.storage.base import KnowledgeBaseStorage
from app.runtime.core.storage.pg_executor import (
    execute_sql,
    query_sql,
    _parse_data,
    _ensure_init,
)

logger = logging.getLogger("lujo-mcp.storage.pg")


class PGKnowledgeBaseStore(KnowledgeBaseStorage):

    def upsert_kb_entry(self, entry: dict) -> None:
        """upsert 一条 KB entry 到 kb_entries 表（按 fingerprint 去重）。"""
        _ensure_init()
        analysis = entry.get("analysis") or {}
        analysis_json = json.dumps(analysis, ensure_ascii=False, default=str)
        now = entry.get("updated_at") or time.time()
        execute_sql(
            """
            INSERT INTO kb_entries
                (fingerprint, analysis, fix_suggestion, source,
                 created_at, updated_at, normalized_fingerprint,
                 type_fingerprint, verify_count, case_confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO UPDATE SET
                analysis               = EXCLUDED.analysis,
                fix_suggestion         = EXCLUDED.fix_suggestion,
                source                 = EXCLUDED.source,
                updated_at             = EXCLUDED.updated_at,
                normalized_fingerprint = EXCLUDED.normalized_fingerprint,
                type_fingerprint       = EXCLUDED.type_fingerprint,
                verify_count           = EXCLUDED.verify_count,
                case_confidence        = EXCLUDED.case_confidence
            """,
            (
                entry.get("fingerprint"),
                analysis_json,
                entry.get("fix_suggestion", ""),
                entry.get("source", ""),
                entry.get("created_at", now),
                now,
                entry.get("normalized_fingerprint", ""),
                entry.get("type_fingerprint", ""),
                entry.get("verify_count", 0),
                entry.get("case_confidence", 0.0),
            ),
        )

    def update_kb_verification(
        self,
        fingerprint: str,
        verify_count: int,
        case_confidence: float,
        updated_at: float,
    ) -> bool:
        """回写验证统计到 kb_entries 表，返回是否命中。"""
        _ensure_init()
        rowcount = execute_sql(
            """
            UPDATE kb_entries
            SET verify_count = %s,
                case_confidence = %s,
                updated_at = %s
            WHERE fingerprint = %s
            """,
            (verify_count, case_confidence, updated_at, fingerprint),
        )
        return rowcount > 0

    def delete_kb_entry(self, fingerprint: str) -> bool:
        """从 kb_entries 表删除一条 entry（LRU 驱逐同步删除）。"""
        _ensure_init()
        rowcount = execute_sql(
            "DELETE FROM kb_entries WHERE fingerprint = %s",
            (fingerprint,),
        )
        return rowcount > 0

    def delete_all_kb_entries(self) -> int:
        """清空 kb_entries 表（clear 同步），返回删除条数。"""
        _ensure_init()
        return execute_sql("DELETE FROM kb_entries")

    def list_recent_kb_entries(self, limit: int = 100) -> list[dict]:
        """按 updated_at 倒序列出最近 limit 条 entry（启动回灌用）。"""
        _ensure_init()
        rows = query_sql(
            """
            SELECT fingerprint, analysis, fix_suggestion, source,
                   created_at, updated_at, normalized_fingerprint,
                   type_fingerprint, verify_count, case_confidence
            FROM kb_entries
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [
            {
                "fingerprint": r[0],
                "analysis": _parse_data(r[1]) or {},
                "fix_suggestion": r[2] or "",
                "source": r[3] or "",
                "created_at": r[4],
                "updated_at": r[5],
                "normalized_fingerprint": r[6] or "",
                "type_fingerprint": r[7] or "",
                "verify_count": r[8] or 0,
                "case_confidence": r[9] or 0.0,
            }
            for r in rows
        ]

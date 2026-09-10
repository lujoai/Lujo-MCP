"""SQLite kb_entries 存储实现 —— KnowledgeBaseStorage ABC 的本地单文件后端（v0.8.0）。

「笔记本」：本地单用户定位下，KB 自有经验的跨重启沉淀由 SQLite 单文件承担，
无需安装任何外部服务（Python 标准库 sqlite3，零新依赖）。

设计要点：
- 表结构对齐 PG 的 kb_entries（fingerprint 主键 + analysis JSON 文本 + 两个索引键），
  使 PG 与 SQLite 两种实现的回灌产物完全一致；
- 每次操作使用短连接（open/close）+ WAL 模式：单用户低写入频率下开销可忽略，
  且天然线程安全（连接不跨线程共享，写穿可能来自 asyncio.to_thread 的工作线程）；
- 与 PG 实现相同的失败语义：异常向上抛，由调用方降级——
  工厂初始化失败时降级 NoOp，运行期写穿失败由 KnowledgeBaseStore 的
  _persist_* 各自 try/except 兜底（不阻断 KB 主流程）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from app.config import settings
from app.runtime.core.storage.base import KnowledgeBaseStorage

logger = logging.getLogger("lujo-mcp.storage.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_entries (
    fingerprint             TEXT PRIMARY KEY,
    analysis                TEXT,
    fix_suggestion          TEXT,
    source                  TEXT,
    created_at              REAL,
    updated_at              REAL,
    normalized_fingerprint  TEXT,
    type_fingerprint        TEXT,
    verify_count            INTEGER DEFAULT 0,
    case_confidence         REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_kb_entries_updated_at
    ON kb_entries (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_entries_normalized
    ON kb_entries (normalized_fingerprint);
CREATE INDEX IF NOT EXISTS idx_kb_entries_type
    ON kb_entries (type_fingerprint);
"""

_SELECT_COLUMNS = (
    "fingerprint, analysis, fix_suggestion, source, created_at, updated_at, "
    "normalized_fingerprint, type_fingerprint, verify_count, case_confidence"
)


class SQLiteKnowledgeBaseStore(KnowledgeBaseStorage):
    """KB 持久化的本地 SQLite 单文件实现（v0.8.0「笔记本」）。"""

    def __init__(self, db_path: str | None = None) -> None:
        raw_path = db_path or settings.kb_persist_path or "lujo-kb.sqlite3"
        # 相对路径解析到当前工作目录（单用户本地自用：工作目录即数据目录）
        self.db_path = str(Path(raw_path).expanduser())
        if self.db_path != ":memory:":
            Path(self.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ── 连接与建表 ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
        except Exception:
            logger.warning("SQLite KB schema 初始化失败 (path=%s)", self.db_path, exc_info=True)
            raise

    # ── KnowledgeBaseStorage 契约 ──

    def upsert_kb_entry(self, entry: dict) -> None:
        """upsert 一条 KB entry（按 fingerprint 去重）。"""
        analysis_json = json.dumps(entry.get("analysis") or {}, ensure_ascii=False, default=str)
        now = entry.get("updated_at") or time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kb_entries
                    (fingerprint, analysis, fix_suggestion, source,
                     created_at, updated_at, normalized_fingerprint,
                     type_fingerprint, verify_count, case_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (fingerprint) DO UPDATE SET
                    analysis               = excluded.analysis,
                    fix_suggestion         = excluded.fix_suggestion,
                    source                 = excluded.source,
                    updated_at             = excluded.updated_at,
                    normalized_fingerprint = excluded.normalized_fingerprint,
                    type_fingerprint       = excluded.type_fingerprint,
                    verify_count           = excluded.verify_count,
                    case_confidence        = excluded.case_confidence
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
        """回写验证统计，返回是否命中。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE kb_entries
                SET verify_count = ?, case_confidence = ?, updated_at = ?
                WHERE fingerprint = ?
                """,
                (verify_count, case_confidence, updated_at, fingerprint),
            )
            return cursor.rowcount > 0

    def delete_kb_entry(self, fingerprint: str) -> bool:
        """删除一条 entry（LRU 驱逐同步删除），返回是否删除成功。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM kb_entries WHERE fingerprint = ?", (fingerprint,)
            )
            return cursor.rowcount > 0

    def delete_all_kb_entries(self) -> int:
        """清空表（clear 同步），返回删除条数。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM kb_entries")
            return cursor.rowcount

    def list_recent_kb_entries(self, limit: int = 100) -> list[dict]:
        """按 updated_at 倒序列出最近 limit 条（启动回灌用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM kb_entries
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    # ── 内部 ──

    @staticmethod
    def _row_to_entry(row: tuple) -> dict:
        analysis_raw = row[1]
        try:
            analysis = json.loads(analysis_raw) if analysis_raw else {}
        except (TypeError, ValueError):
            analysis = {}
        return {
            "fingerprint": row[0],
            "analysis": analysis if isinstance(analysis, dict) else {},
            "fix_suggestion": row[2] or "",
            "source": row[3] or "",
            "created_at": row[4],
            "updated_at": row[5],
            "normalized_fingerprint": row[6] or "",
            "type_fingerprint": row[7] or "",
            "verify_count": row[8] or 0,
            "case_confidence": row[9] or 0.0,
        }

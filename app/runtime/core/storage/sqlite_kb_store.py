"""SQLite kb_entries 存储实现 —— KnowledgeBaseStorage ABC 的本地单文件后端（v0.8.0）。

「笔记本」：本地单用户定位下，KB 自有经验的跨重启沉淀由 SQLite 单文件承担，
无需安装任何外部服务（Python 标准库 sqlite3，零新依赖）。

设计要点：
- 表结构沿用历史 PG 版 kb_entries 的形状（fingerprint 主键 + analysis JSON 文本
  + 两个索引键）。W13 文案更正：PostgreSQL 运行时后端已随 Step 3 移除，
  本实现是**唯一**的 KB 持久化后端，不再有「两种实现回灌产物一致」的对照面；
- 每次操作使用短连接 + WAL 模式：单用户低写入频率下开销可忽略，
  且天然线程安全（连接不跨线程共享，写穿可能来自 asyncio.to_thread 的工作线程）；
- 失败语义：异常向上抛，由调用方降级——工厂初始化失败时降级 NoOp（该降级由
  ``factory.kb_persist_degraded()`` 暴露给 health，W13 / P3-STORE-4），运行期
  写穿失败由 KnowledgeBaseStore 的 _persist_* 各自 try/except 兜底（不阻断 KB
  主流程）。

⚠️ 已知边界（W13 / P4-存储，登记不修）：表结构**没有 schema 版本号，也没有迁移
入口**。加列/改列只能靠「新建表 + 拷贝」或让用户删掉笔记本文件重来；而删除
PostgreSQL 时连带的迁移入口也已移除（Step 3 WP6），所以此处不存在可复用的迁移
框架。引入版本化迁移属新增持久化面，须单独立项（并同步 Step 3 的既有决策），
不在维护包里顺手做。当前风险可接受：字段自 v0.8.0 起未变，且回灌对缺字段
用 ``row.get(...)`` 兜底。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
        raw_path = str(db_path or settings.kb_persist_path or "lujo-kb.sqlite3").strip()
        # 显式拒绝 :memory:：本实现用短连接，而 SQLite 的内存库是「每连接独立」的，
        # 建表与后续操作会落在不同的空库上（恒 no such table），无法持久化。
        if raw_path == ":memory:":
            raise ValueError(
                "kb_persist_path 不支持 ':memory:'：SQLiteKnowledgeBaseStore 使用短连接，"
                "每连接独立的内存库无法承载持久化。请改用文件路径，"
                "或设置 KB_PERSIST_ENABLED=false 关闭持久化。"
            )
        # 解析为绝对路径：相对路径按「构造时」的工作目录定格，避免运行期 cwd 变化导致落库位置漂移
        self.db_path = str(Path(raw_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ── 连接与建表 ──

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """短连接上下文：提交/回滚后**显式关闭**（sqlite3 的 `with conn` 只管事务不关闭连接）。"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        try:
            with self._connection() as conn:
                conn.executescript(_SCHEMA)
        except Exception:
            logger.warning("SQLite KB schema 初始化失败 (path=%s)", self.db_path, exc_info=True)
            raise

    # ── KnowledgeBaseStorage 契约 ──

    def upsert_kb_entry(self, entry: dict) -> None:
        """upsert 一条 KB entry（按 fingerprint 去重）。"""
        analysis_json = json.dumps(entry.get("analysis") or {}, ensure_ascii=False, default=str)
        now = entry.get("updated_at") or time.time()
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM kb_entries WHERE fingerprint = ?", (fingerprint,)
            )
            return cursor.rowcount > 0

    def delete_all_kb_entries(self) -> int:
        """清空表（clear 同步），返回删除条数。"""
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM kb_entries")
            return cursor.rowcount

    def list_recent_kb_entries(self, limit: int = 100) -> list[dict]:
        """按 updated_at 倒序列出最近 limit 条（启动回灌用）。"""
        with self._connection() as conn:
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

    def checkpoint(self) -> None:
        """WAL 收口：把 WAL 日志合并回主文件并截断（Step 3 WP2 迁移工具新增）。

        供 factory 的一次性迁移入口在复制/备份目标文件前调用：仅复制主文件时
        若 WAL 尚有未合并帧，备份会缺数据。本实现每次操作用短连接、正常关闭时
        SQLite 已自动 checkpoint，这里是显式兜底；不属于
        KnowledgeBaseStorage ABC 契约，运行时写穿路径无需调用。
        """
        with self._connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

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

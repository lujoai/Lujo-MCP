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
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings
from app.runtime.core.storage.base import KnowledgeBaseStorage

logger = logging.getLogger("lujo-mcp.storage.sqlite")

_DEFAULT_KB_MIGRATION_MARKER = ".lujo-kb-cwd-migration-complete"
_REQUIRED_KB_COLUMNS = frozenset(
    {
        "fingerprint",
        "analysis",
        "fix_suggestion",
        "source",
        "created_at",
        "updated_at",
        "normalized_fingerprint",
        "type_fingerprint",
        "verify_count",
        "case_confidence",
    }
)


def get_default_kb_persist_path() -> Path:
    """确定跨平台默认用户数据目录下的 SQLite 笔记本持久化路径。

    - Windows: %LOCALAPPDATA%\\lujo-mcp\\lujo-kb.sqlite3（若无 LOCALAPPDATA 环境变量则回退 ~/.local/share/lujo-mcp/lujo-kb.sqlite3）
    - macOS: ~/Library/Application Support/lujo-mcp/lujo-kb.sqlite3
    - Linux/其他: $XDG_DATA_HOME/lujo-mcp/lujo-kb.sqlite3（若无则回退 ~/.local/share/lujo-mcp/lujo-kb.sqlite3）
    """
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            base_dir = Path(local_appdata).expanduser()
        else:
            base_dir = Path.home() / ".local" / "share"
    elif sys.platform == "darwin":
        base_dir = Path.home() / "Library" / "Application Support"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data:
            base_dir = Path(xdg_data).expanduser()
        else:
            base_dir = Path.home() / ".local" / "share"
    return (base_dir / "lujo-mcp" / "lujo-kb.sqlite3").resolve()


def _mark_default_kb_initialized(target_path: Path) -> None:
    """记住默认目录已初始化，防止删除新库后再次导入保留的旧 CWD 库。"""
    marker = target_path.with_name(_DEFAULT_KB_MIGRATION_MARKER)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not marker.is_file():
            raise OSError(f"SQLite KB 迁移标记不是普通文件: {marker}") from None
        return

    try:
        with os.fdopen(fd, "wb") as marker_file:
            marker_file.write(b"v1\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())
    except Exception:
        marker.unlink(missing_ok=True)
        raise


def is_valid_sqlite_kb(candidate_path: Path | str) -> bool:
    """校验候选文件为完整 SQLite KB 库，包含所需字段及单列 fingerprint 主键。"""
    path = Path(candidate_path)
    if not path.is_file():
        return False
    # SQLite 最小文件大小为 100 字节，且必须以魔数开头
    try:
        if path.stat().st_size < 100:
            return False
        with open(path, "rb") as f:
            header = f.read(16)
        if header != b"SQLite format 3\x00":
            return False
    except Exception:
        return False

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_entries'"
        )
        if cursor.fetchone() is None:
            return False

        table_info = cursor.execute("PRAGMA table_info(kb_entries)").fetchall()
        column_names = {str(row[1]).casefold() for row in table_info}
        missing_columns = _REQUIRED_KB_COLUMNS - column_names
        primary_key_columns = [
            str(row[1]).casefold() for row in table_info if int(row[5]) > 0
        ]
        if missing_columns or primary_key_columns != ["fingerprint"]:
            logger.debug(
                "文件 %s kb_entries schema 不兼容: missing_columns=%s, primary_key=%s",
                path,
                sorted(missing_columns),
                primary_key_columns,
            )
            return False

        row = cursor.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            return False
        return True
    except Exception as e:
        logger.debug("文件 %s SQLite 有效性校验未通过: %s", path, e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _migrate_legacy_cwd_kb_if_needed(target_path: Path) -> bool:
    """非破坏性安全迁移：将 CWD 下的旧库做一致性快照并保留原文件。

    使用 SQLite backup API 读取主库及 WAL 中的同一事务快照，先写入目标目录
    的临时文件并校验，再以不覆盖目标的硬链接原子发布，避免复制中断留下半成品。
    发布后记录一次性标记；若恰在两步之间崩溃，下一次启动会由已存在的
    目标库补记标记。有效旧库的快照/发布失败则抛错，避免空目标阻断重试。
    """
    target_path = target_path.resolve()
    if target_path.exists() or target_path.with_name(_DEFAULT_KB_MIGRATION_MARKER).exists():
        return False

    cwd_candidate = (Path.cwd() / "lujo-kb.sqlite3").resolve()
    if not cwd_candidate.is_file():
        return False

    # 若 CWD 路径与目标路径恰好相同，不做自复制
    if cwd_candidate == target_path.resolve():
        return False

    if not is_valid_sqlite_kb(cwd_candidate):
        logger.info(
            "CWD 下存在旧版数据库文件 %s，但有效性校验未通过（损坏或非法结构），跳过迁移",
            cwd_candidate,
        )
        return False

    temp_path: Path | None = None
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.migrate-",
            suffix=".tmp",
            dir=target_path.parent,
        )
        temp_path = Path(temp_name)
        os.close(fd)

        source_conn: sqlite3.Connection | None = None
        target_conn: sqlite3.Connection | None = None
        try:
            source_uri = f"{cwd_candidate.as_uri()}?mode=ro"
            source_conn = sqlite3.connect(source_uri, uri=True, timeout=2.0)
            target_conn = sqlite3.connect(str(temp_path), timeout=2.0)
            source_conn.backup(target_conn)
        finally:
            if target_conn is not None:
                target_conn.close()
            if source_conn is not None:
                source_conn.close()

        if not is_valid_sqlite_kb(temp_path):
            raise ValueError(f"从 CWD {cwd_candidate} 创建的 SQLite 快照校验失败")

        try:
            # Same-directory hard link publishes the completed snapshot atomically and
            # fails if another instance has already created the destination.
            os.link(temp_path, target_path)
        except FileExistsError:
            logger.info("另一 Lujo 实例已创建目标笔记本 %s，跳过旧库迁移", target_path)
            return False

        _mark_default_kb_initialized(target_path)
        logger.info(
            "已将当前工作目录旧版数据库从 %s 一致性迁移至用户数据目录 %s，原文件已保留",
            cwd_candidate,
            target_path,
        )
        return True
    except Exception as e:
        logger.warning(
            "从 CWD %s 迁移数据库至 %s 失败: %s，原文件保留；本次禁用 KB 持久化以便下次重试",
            cwd_candidate,
            target_path,
            e,
        )
        raise
    finally:
        if temp_path is not None:
            for suffix in ("", "-wal", "-shm"):
                try:
                    temp_path.with_name(temp_path.name + suffix).unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.debug("清理 SQLite 迁移临时文件失败: %s", cleanup_error)


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
        explicit_path: str | None = None
        if db_path is not None and str(db_path).strip():
            explicit_path = str(db_path).strip()
        elif settings.kb_persist_path and str(settings.kb_persist_path).strip():
            explicit_path = str(settings.kb_persist_path).strip()

        if explicit_path is not None:
            # 显式配置：100% 优先尊重用户配置，不进行任何重定向或隐式迁移
            # 显式拒绝 :memory:：本实现用短连接，而 SQLite 的内存库是「每连接独立」的，
            # 建表与后续操作会落在不同的空库上（恒 no such table），无法持久化。
            if explicit_path == ":memory:":
                raise ValueError(
                    "kb_persist_path 不支持 ':memory:'：SQLiteKnowledgeBaseStore 使用短连接，"
                    "每连接独立的内存库无法承载持久化。请改用文件路径，"
                    "或设置 KB_PERSIST_ENABLED=false 关闭持久化。"
                )
            # 解析为绝对路径：相对路径按「构造时」的工作目录定格，避免运行期 cwd 变化导致落库位置漂移
            self.db_path = str(Path(explicit_path).expanduser().resolve())
        else:
            # 默认路径：使用跨平台用户数据目录，并执行非破坏性迁移检测
            target_path = get_default_kb_persist_path()
            self.db_path = str(target_path)
            _migrate_legacy_cwd_kb_if_needed(target_path)

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        if explicit_path is None:
            _mark_default_kb_initialized(target_path)

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

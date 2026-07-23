"""PostgreSQL 存储 —— 线程安全连接池 + 自动重连 + 优雅关闭"""

import json
import time
import threading
import logging
from typing import Optional

import psycopg2
import psycopg2.pool
import psycopg2.errors

from app.config import settings
from app.mcp.core.storage.base import TraceStorage, SessionStorage

logger = logging.getLogger("ai-debug-mcp.storage.pg")


def _parse_data(value):
    """安全解析 data 字段：处理 None / dict / list / JSON 字符串 / 普通字符串"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


# ── 全局连接池（线程安全初始化） ──
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()
_initialized = False
_init_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    _pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=settings.pg_min_connections,
                        maxconn=settings.pg_max_connections,
                        host=settings.pg_host,
                        port=settings.pg_port,
                        dbname=settings.pg_database,
                        user=settings.pg_user,
                        password=settings.pg_password,
                        connect_timeout=5,
                    )
                    logger.info(
                        "PostgreSQL 连接池已创建 (min=%d, max=%d)",
                        settings.pg_min_connections,
                        settings.pg_max_connections,
                    )
                except psycopg2.OperationalError as e:
                    logger.critical(f"PostgreSQL 连接失败: {e}")
                    raise RuntimeError(f"无法连接 PostgreSQL: {e}")
    return _pool


def close_pool() -> None:
    """优雅关闭连接池（在 lifespan shutdown 中调用）"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
                logger.info("PostgreSQL 连接池已关闭")
            except Exception as e:
                logger.warning(f"关闭 PG 连接池时出错: {e}")
            _pool = None


# ── 建表 DDL ──
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

DDL_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  DOUBLE PRECISION NOT NULL,
    last_active DOUBLE PRECISION NOT NULL,
    metadata    JSONB
);
CREATE INDEX IF NOT EXISTS idx_sessions_la ON sessions(last_active);
"""

# Phase 2.3：errors 表持久化聚合（按 fingerprint+session_id upsert，冲突时 occurrence_count+=1）
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

# Phase 2.4：specs 表独立查询（消除 N+1 扫描）
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


def _ensure_init():
    """确保表已就绪（仅初始化一次）"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(DDL_TRACES)
            cur.execute(DDL_SESSIONS)
            cur.execute(DDL_ERRORS)
            cur.execute(DDL_SPECS)
            conn.commit()
            _initialized = True
            logger.info("PostgreSQL 表初始化完成")
        finally:
            pool.putconn(conn)


# ── 带重试的 SQL 执行装饰器 ──
def _execute_with_retry(
    conn,
    sql: str,
    params: tuple = (),
    max_retries: int = 2,
    commit: bool = True,
):
    """执行 SQL，遇到连接断开时自动重连重试。

    SEC-14 修复：OperationalError 时获取新连接并重试，而不是在旧连接上重试。
    坏连接使用 putconn(close=True) 关闭，避免污染连接池。

    返回 (conn, rowcount)：
    - conn: 最新的连接对象（可能是重连后的新连接）
    - rowcount: cursor.rowcount（用于 DELETE/INSERT/UPDATE 的行数统计）
    """
    last_error = None
    pool = _get_pool()
    rowcount = 0
    for attempt in range(max_retries + 1):
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rowcount = cur.rowcount
            if commit:
                conn.commit()
            return conn, rowcount
        except psycopg2.OperationalError as e:
            last_error = e
            if attempt < max_retries:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = pool.getconn()
                logger.warning(f"PG 操作重试 ({attempt + 1}/{max_retries}): {e}")
                time.sleep(0.1)
            else:
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
                raise last_error


# ════════════════════════════════════════════
#  Trace 存储
# ════════════════════════════════════════════
class PGTraceStore(TraceStorage):
    def __init__(self):
        _ensure_init()

    def _conn(self):
        return _get_pool().getconn()

    def _put(self, conn):
        _get_pool().putconn(conn)

    def save_entry(self, request_id: str, entry: dict) -> None:
        conn = self._conn()
        try:
            data = entry.get("data")
            if data is None:
                data_str = None
            elif isinstance(data, (str, int, float, bool, list, dict)):
                data_str = json.dumps(data, ensure_ascii=False, default=str)
            else:
                data_str = json.dumps(str(data), ensure_ascii=False)

            conn, _ = _execute_with_retry(
                conn,
                "INSERT INTO traces (request_id, timestamp, step, data) VALUES (%s, %s, %s, %s)",
                (
                    request_id,
                    entry.get("timestamp", time.time()),
                    entry.get("step", ""),
                    data_str,
                ),
            )
        finally:
            self._put(conn)

    def get_entries(self, request_id: str) -> list[dict]:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT timestamp, step, data FROM traces WHERE request_id = %s ORDER BY timestamp",
                (request_id,),
            )
            rows = cur.fetchall()
            return [
                {
                    "timestamp": r[0],
                    "step": r[1],
                    "data": _parse_data(r[2]),
                }
                for r in rows
            ]
        finally:
            self._put(conn)

    def delete(self, request_id: str) -> None:
        conn = self._conn()
        try:
            conn, _ = _execute_with_retry(
                conn,
                "DELETE FROM traces WHERE request_id = %s",
                (request_id,),
            )
        finally:
            self._put(conn)

    def cleanup_expired(self, ttl_seconds: int) -> int:
        conn = self._conn()
        try:
            cutoff = time.time() - ttl_seconds
            conn, rowcount = _execute_with_retry(
                conn,
                "DELETE FROM traces WHERE request_id IN ("
                "  SELECT request_id FROM traces "
                "  GROUP BY request_id HAVING MAX(timestamp) < %s"
                ")",
                (cutoff,),
            )
            return rowcount
        finally:
            self._put(conn)

    def list_request_ids(self, limit: int = 50) -> list[str]:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT request_id FROM ("
                "  SELECT request_id, MAX(timestamp) as max_ts FROM traces GROUP BY request_id"
                ") t ORDER BY max_ts DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
            return [r[0] for r in rows]
        finally:
            self._put(conn)


# ════════════════════════════════════════════════
#  Session 存储
# ════════════════════════════════════════════════
class PGSessionStore(SessionStorage):
    def __init__(self):
        _ensure_init()

    def _conn(self):
        return _get_pool().getconn()

    def _put(self, conn):
        _get_pool().putconn(conn)

    def save(self, session_id: str, data: dict) -> None:
        conn = self._conn()
        try:
            data["last_active"] = time.time()
            conn, _ = _execute_with_retry(
                conn,
                "INSERT INTO sessions (session_id, created_at, last_active, metadata) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (session_id) DO UPDATE SET "
                "  last_active = EXCLUDED.last_active,"
                "  metadata    = EXCLUDED.metadata",
                (
                    session_id,
                    data.get("created_at", time.time()),
                    data["last_active"],
                    json.dumps(data.get("metadata", {})),
                ),
            )
        finally:
            self._put(conn)

    def get(self, session_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT session_id, created_at, last_active, metadata FROM sessions WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE sessions SET last_active = %s WHERE session_id = %s",
                (time.time(), session_id),
            )
            conn.commit()
            return {
                "session_id": row[0],
                "created_at": row[1],
                "last_active": row[2],
                "metadata": json.loads(row[3]) if isinstance(row[3], str) else row[3],
            }
        finally:
            self._put(conn)

    def delete(self, session_id: str) -> None:
        conn = self._conn()
        try:
            conn, _ = _execute_with_retry(
                conn,
                "DELETE FROM sessions WHERE session_id = %s",
                (session_id,),
            )
        finally:
            self._put(conn)

    def list_active(self, ttl_seconds: int) -> list[dict]:
        conn = self._conn()
        try:
            cutoff = time.time() - ttl_seconds
            cur = conn.cursor()
            cur.execute(
                "SELECT session_id, created_at, last_active, metadata FROM sessions WHERE last_active > %s",
                (cutoff,),
            )
            rows = cur.fetchall()
            return [
                {
                    "session_id": r[0],
                    "created_at": r[1],
                    "last_active": r[2],
                    "metadata": json.loads(r[3]) if isinstance(r[3], str) else r[3],
                }
                for r in rows
            ]
        finally:
            self._put(conn)

    def cleanup_expired(self, ttl_seconds: int) -> int:
        conn = self._conn()
        try:
            cutoff = time.time() - ttl_seconds
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE last_active < %s", (cutoff,))
            count = cur.rowcount
            conn.commit()
            return count
        finally:
            self._put(conn)


# ════════════════════════════════════════════════════
#  Phase 2.3：errors 表 CRUD（持久化聚合）
#  按 (fingerprint, session_id) upsert，冲突时 occurrence_count += 1
# ════════════════════════════════════════════════════
def upsert_error(record_data: dict) -> None:
    """upsert 一条错误记录到 errors 表。

    按 (fingerprint, session_id) 去重：
    - 不存在 → INSERT（occurrence_count=1）
    - 已存在 → occurrence_count += 1，刷新 last_seen/message/frames 等

    session_id 为 None 时写入 "_global"，与 errors 内存分桶逻辑一致。
    """
    _ensure_init()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        frames = record_data.get("frames")
        frames_json = (
            json.dumps(frames, ensure_ascii=False, default=str)
            if frames is not None
            else None
        )
        session_id = record_data.get("session_id") or "_global"
        now = record_data.get("last_seen") or time.time()
        conn, _ = _execute_with_retry(
            conn,
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
    finally:
        pool.putconn(conn)


# ════════════════════════════════════════════════════
#  Phase 2.4：specs 表 CRUD（独立查询，消除 N+1）
# ════════════════════════════════════════════════════
def save_spec(spec: dict) -> None:
    """upsert 一条 spec 到 specs 表（按 id 去重）。"""
    _ensure_init()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        expect = spec.get("expect") or {}
        expect_json = json.dumps(expect, ensure_ascii=False, default=str)
        now = spec.get("updated_at") or time.time()
        conn, _ = _execute_with_retry(
            conn,
            """
            INSERT INTO specs (id, kind, target, expect, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                kind       = EXCLUDED.kind,
                target     = EXCLUDED.target,
                expect     = EXCLUDED.expect,
                updated_at = EXCLUDED.updated_at
            """,
            (
                spec.get("id"),
                spec.get("kind", "api"),
                spec.get("target", ""),
                expect_json,
                spec.get("created_at", now),
                now,
            ),
        )
    finally:
        pool.putconn(conn)


def get_spec(spec_id: str) -> Optional[dict]:
    """从 specs 表读取一条 spec。"""
    _ensure_init()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, kind, target, expect, created_at, updated_at "
            "FROM specs WHERE id = %s",
            (spec_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "kind": row[1],
            "target": row[2],
            "expect": _parse_data(row[3]),
            "created_at": row[4],
            "updated_at": row[5],
        }
    finally:
        pool.putconn(conn)


def list_specs_pg(
    kind: Optional[str] = None,
    target: Optional[str] = None,
) -> list[dict]:
    """从 specs 表读取所有 spec（可按 kind/target 过滤），按 updated_at 倒序。"""
    _ensure_init()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        sql = (
            "SELECT id, kind, target, expect, created_at, updated_at FROM specs"
        )
        params: list = []
        conditions: list[str] = []
        if kind:
            conditions.append("kind = %s")
            params.append(kind)
        if target:
            conditions.append("target LIKE %s")
            params.append(f"%{target}%")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY updated_at DESC"
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "target": r[2],
                "expect": _parse_data(r[3]),
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]
    finally:
        pool.putconn(conn)


def delete_spec(spec_id: str) -> bool:
    """从 specs 表删除一条 spec，返回是否删除成功。"""
    _ensure_init()
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn, rowcount = _execute_with_retry(
            conn,
            "DELETE FROM specs WHERE id = %s",
            (spec_id,),
        )
        return rowcount > 0
    finally:
        pool.putconn(conn)

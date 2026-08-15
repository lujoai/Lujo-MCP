"""PostgreSQL 存储 —— 线程安全连接池 + 自动重连 + 优雅关闭 + 熔断器"""

import json
import time
import threading
import logging
from typing import Optional

import psycopg2
import psycopg2.pool
import psycopg2.errors

from app.config import settings
from app.runtime.core.storage.base import TraceStorage, SessionStorage, ErrorStorage, SpecStorage
from app.runtime.core.storage._pg_errors import sanitize_pg_error
from app.runtime.core.storage.ddl import (  # FIX: P0-5 DDL 单源，消除与 async_pg_store/migrations 分叉
    DDL_TRACES,
    DDL_SESSIONS,
    DDL_ERRORS,
    DDL_SPECS,
    DDL_TRACES_ARCHIVE,
)

logger = logging.getLogger("lujo-mcp.storage.pg")

# ── 熔断器（P3-8）──
try:
    import pybreaker
except ImportError:
    pybreaker = None
    logger.warning("pybreaker 未安装，熔断器功能已禁用")


_pg_circuit_breaker = None
_pg_circuit_breaker_lock = threading.Lock()


def _get_pg_circuit_breaker():
    global _pg_circuit_breaker
    if _pg_circuit_breaker is not None:
        return _pg_circuit_breaker
    if not pybreaker or not settings.circuit_breaker_enabled:
        return None
    with _pg_circuit_breaker_lock:
        if _pg_circuit_breaker is None:
            _pg_circuit_breaker = pybreaker.CircuitBreaker(
                fail_max=settings.cb_pg_max_failures,
                reset_timeout=settings.cb_pg_reset_timeout,
                exclude=[pybreaker.CircuitBreakerError],
            )
    return _pg_circuit_breaker


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
                    # 完整细节（含 DSN 参数）进日志，便于排障；传播的错误只含脱敏摘要（N4-FU-3）
                    logger.critical("PostgreSQL 连接失败: %s", e)
                    raise RuntimeError(sanitize_pg_error(e)) from None
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


def _get_conn(timeout: float = 5.0):
    """从连接池取连接；池耗尽时等待有界时间，超时抛 PoolError。

    FIX: P1-9d ThreadedConnectionPool.getconn() 无超时参数，全占用时
    永久阻塞；配合 _execute_with_retry 的重试会进一步加剧。统一经本函数
    获取连接，超时抛可识别的 PoolError，由上层计入熔断/降级路径
    （storage_fallback_to_memory）。
    """
    pool = _get_pool()
    deadline = time.time() + timeout
    while True:
        try:
            # FIX: P1-9d 取连接必须是 pool.getconn()（此前误写成递归调用
            # 自身 _get_conn()，池耗尽时无限递归 RecursionError）
            return pool.getconn()
        except psycopg2.pool.PoolError:
            if time.time() >= deadline:
                logger.error(
                    "PG 连接池耗尽，等待 %.1fs 超时（maxconn=%d）",
                    timeout,
                    settings.pg_max_connections,
                )
                raise
            time.sleep(0.05)


# ── 建表 DDL ──
# FIX: P0-5 DDL 已抽取到 app/runtime/core/storage/ddl.py（与 async_pg_store 共享单源）


def _month_partition_name(year: int, month: int) -> str:
    """生成分区表名：traces_YYYY_MM"""
    return f"traces_{year:04d}_{month:02d}"


def _month_range_epoch(year: int, month: int) -> tuple[float, float]:
    """计算某月的起止 unix 时间戳（秒，用于 DOUBLE PRECISION timestamp 字段）。
    返回 (start_ts, end_ts)，区间为 [start, end)。
    """
    from datetime import datetime, timezone

    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    end_dt = datetime(next_year, next_month, 1, tzinfo=timezone.utc)
    return start_dt.timestamp(), end_dt.timestamp()


def _create_partition_for_month(conn, year: int, month: int) -> bool:
    """为指定年月创建 traces 表的 RANGE 分区。
    如果分区已存在则返回 False，创建成功返回 True。
    """
    part_name = _month_partition_name(year, month)
    start_ts, end_ts = _month_range_epoch(year, month)

    # 先检查分区是否已存在（避免 IF NOT EXISTS 在 CREATE TABLE ... PARTITION OF 中不可用的问题）
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM pg_tables WHERE tablename = %s",
        (part_name,),
    )
    if cur.fetchone() is not None:
        return False

    sql = (
        f"CREATE TABLE {part_name} "
        f"PARTITION OF traces FOR VALUES FROM ({start_ts}) TO ({end_ts})"
    )
    cur.execute(sql)
    logger.info("已创建分区: %s (%.0f ~ %.0f)", part_name, start_ts, end_ts)
    return True


def _ensure_partitions(conn) -> int:
    """确保当月及未来 N 个月的分区存在。返回新创建的分区数量。
    仅在 settings.pg_partition_enabled=True 时生效。
    """
    if not settings.pg_partition_enabled:
        return 0

    # FIX: P1-9c 若 traces 已是普通表（relkind='r'），直接建分区必然报
    # "is not partitioned"，导致每次启动崩溃。检测到非分区表时跳过建分区
    # 并告警（保持原表不变，符合"已存在普通表不转换"的设计注释）。
    cur = conn.cursor()
    cur.execute(
        "SELECT c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE c.relname = %s AND n.nspname = current_schema()",
        ("traces",),
    )
    row = cur.fetchone()
    if row is not None and row[0] != "p":
        logger.warning(
            "PG partition enabled 但 traces 表已存在且非分区表（relkind=%s），"
            "跳过建分区保持原表不变；如需分区请手动 ALTER 转换或重建库",
            row[0],
        )
        return 0

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    created = 0
    precreate = max(0, settings.pg_partition_precreate_months)

    for i in range(precreate + 1):  # +1 包含当月
        y = now.year
        m = now.month + i
        while m > 12:
            y += 1
            m -= 12
        if _create_partition_for_month(conn, y, m):
            created += 1

    return created


def _archive_old_traces(conn, days: int) -> int:
    """将超过 days 天的 traces 数据归档到 traces_archive 表。
    返回归档的行数。仅在 settings.pg_archive_enabled=True 时生效。
    """
    if not settings.pg_archive_enabled:
        return 0

    cutoff = time.time() - days * 86400
    cur = conn.cursor()

    if settings.pg_archive_delete_after:
        cur.execute(
            "WITH moved AS ("
            "  DELETE FROM traces WHERE timestamp < %s "
            "  RETURNING id, request_id, timestamp, step, data"
            ") "
            "INSERT INTO traces_archive (id, request_id, timestamp, step, data) "
            "SELECT id, request_id, timestamp, step, data FROM moved",
            (cutoff,),
        )
    else:
        cur.execute(
            "INSERT INTO traces_archive (id, request_id, timestamp, step, data) "
            "SELECT id, request_id, timestamp, step, data FROM traces "
            "WHERE timestamp < %s AND id NOT IN (SELECT id FROM traces_archive)",
            (cutoff,),
        )
    count = cur.rowcount
    if count > 0:
        logger.info("已归档 %d 条 traces 数据到 traces_archive (>%d天)", count, days)
    return count


def _ensure_init():
    """确保表已就绪（仅初始化一次）"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        pool = _get_pool()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            # P3-1：分区模式下用分区表替代普通表
            if settings.pg_partition_enabled:
                # 检查 traces 是否已经是分区表
                cur.execute(
                    "SELECT 1 FROM pg_tables WHERE tablename = 'traces'"
                )
                if cur.fetchone() is None:
                    # 全新建表：直接建成分区表
                    cur.execute("""
                        CREATE TABLE traces (
                            id          BIGSERIAL,
                            request_id  TEXT        NOT NULL,
                            timestamp   DOUBLE PRECISION NOT NULL,
                            step        TEXT        NOT NULL,
                            data        JSONB,
                            PRIMARY KEY (id, timestamp)
                        ) PARTITION BY RANGE (timestamp)
                    """)
                    cur.execute("CREATE INDEX idx_traces_rid ON traces(request_id)")
                    cur.execute("CREATE INDEX idx_traces_ts  ON traces(timestamp)")
                    logger.info("已创建分区表 traces (RANGE BY timestamp)")
                # 已存在表但不是分区表 → 不自动转换，保持原状（避免数据迁移风险）
            else:
                cur.execute(DDL_TRACES)

            cur.execute(DDL_SESSIONS)
            cur.execute(DDL_ERRORS)
            cur.execute(DDL_SPECS)

            # P3-2：归档表
            if settings.pg_archive_enabled:
                cur.execute(DDL_TRACES_ARCHIVE)

            conn.commit()

            # P3-1：确保分区存在
            if settings.pg_partition_enabled:
                new_parts = _ensure_partitions(conn)
                if new_parts > 0:
                    conn.commit()

            _initialized = True
            logger.info("PostgreSQL 表初始化完成")
        finally:
            pool.putconn(conn)


# ── 带重试和熔断器的 SQL 执行 ──
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

    P3-8: 熔断器保护，当 PG 连续失败时触发熔断。

    返回 (conn, rowcount)：
    - conn: 最新的连接对象（可能是重连后的新连接）
    - rowcount: cursor.rowcount（用于 DELETE/INSERT/UPDATE 的行数统计）
    """

    def _do_execute():
        nonlocal conn
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
                    conn = _get_conn()
                    logger.warning(f"PG 操作重试 ({attempt + 1}/{max_retries}): {e}")
                    time.sleep(0.1)
                else:
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                    raise last_error

    cb = _get_pg_circuit_breaker()
    if cb:
        try:
            return cb.call(_do_execute)
        except pybreaker.CircuitBreakerError:
            logger.warning("PG 熔断器已触发")
            raise
    return _do_execute()


def _query_with_retry(
    conn,
    sql: str,
    params: tuple = (),
    fetch_all: bool = True,
    max_retries: int = 2,
):
    """执行查询 SQL（SELECT），遇到连接断开时自动重连重试，受熔断器保护。

    P3-8: 熔断器保护，当 PG 连续失败时触发熔断。

    与 _execute_with_retry 对齐读路径重连重试：OperationalError 时丢弃并
    获取新连接重试，避免在坏连接上重复查询。坏连接 putconn(close=True) 关闭，
    避免污染连接池。

    返回: fetch_all=True 返回所有行列表，fetch_all=False 返回单行
    """

    def _do_query():
        nonlocal conn
        pool = _get_pool()
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                if fetch_all:
                    return cur.fetchall()
                return cur.fetchone()
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
                    conn = _get_conn()
                    logger.warning(f"PG 查询重试 ({attempt + 1}/{max_retries}): {e}")
                    time.sleep(0.1)
                else:
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                    raise last_error

    cb = _get_pg_circuit_breaker()
    if cb:
        try:
            return cb.call(_do_query)
        except pybreaker.CircuitBreakerError:
            logger.warning("PG 熔断器已触发（查询）")
            raise
    return _do_query()


# ════════════════════════════════════════════
#  Trace 存储
# ════════════════════════════════════════════
class PGTraceStore(TraceStorage):
    def __init__(self):
        _ensure_init()

    def ping(self) -> bool:
        """真实探活：SELECT 1。失败返回 False 而非抛异常（A1）。"""
        try:
            _ensure_init()
            conn = _get_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                conn.commit()
                return True
            finally:
                self._put(conn)
        except Exception:
            logger.debug("PG 探活失败", exc_info=True)
            return False

    def _conn(self):
        return _get_conn()

    def _put(self, conn):
        if conn is not None and not conn.closed:
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

            # P3-1：每次写入时惰性检查是否需要创建新分区（低概率路径，不影响性能）
            if settings.pg_partition_enabled and self._should_check_partitions():
                try:
                    _ensure_partitions(conn)
                    conn.commit()
                except Exception as e:
                    logger.warning("分区预创建失败（不影响写入）: %s", e)
        finally:
            self._put(conn)

    def _should_check_partitions(self) -> bool:
        """惰性分区检查：每 1000 次写入检查一次，避免每次都调用。"""
        if not hasattr(self, "_write_counter"):
            self._write_counter = 0
        self._write_counter += 1
        if self._write_counter >= 1000:
            self._write_counter = 0
            return True
        return False

    def get_entries(self, request_id: str) -> list[dict]:
        conn = self._conn()
        try:
            rows = _query_with_retry(
                conn,
                "SELECT timestamp, step, data FROM traces WHERE request_id = %s ORDER BY timestamp",
                (request_id,),
            )
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
            # P3-2：先执行归档（超过 pg_archive_days 天的数据移到归档表）
            archived = 0
            if settings.pg_archive_enabled:
                try:
                    archived = _archive_old_traces(conn, settings.pg_archive_days)
                    if archived > 0:
                        conn.commit()
                except Exception as e:
                    logger.warning("归档失败，继续执行过期清理: %s", e)
                    conn.rollback()

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
            rows = _query_with_retry(
                conn,
                "SELECT request_id FROM ("
                "  SELECT request_id, MAX(timestamp) as max_ts FROM traces GROUP BY request_id"
                ") t ORDER BY max_ts DESC LIMIT %s",
                (limit,),
            )
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
        return _get_conn()

    def _put(self, conn):
        if conn is not None and not conn.closed:
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
            row = _query_with_retry(
                conn,
                "SELECT session_id, created_at, last_active, metadata FROM sessions WHERE session_id = %s",
                (session_id,),
                fetch_all=False,
            )
            if row is None:
                return None
            conn, _ = _execute_with_retry(
                conn,
                "UPDATE sessions SET last_active = %s WHERE session_id = %s",
                (time.time(), session_id),
            )
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
            rows = _query_with_retry(
                conn,
                "SELECT session_id, created_at, last_active, metadata FROM sessions WHERE last_active > %s",
                (cutoff,),
            )
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
            conn, rowcount = _execute_with_retry(
                conn,
                "DELETE FROM sessions WHERE last_active < %s",
                (cutoff,),
            )
            return rowcount
        finally:
            self._put(conn)


# ════════════════════════════════════════════════════
#  Phase 2.3：errors 表 CRUD（持久化聚合）—— ErrorStorage ABC 实现
#  按 (fingerprint, session_id) upsert，冲突时 occurrence_count += 1
# ════════════════════════════════════════════════════
class PGErrorStore(ErrorStorage):

    def upsert_error(self, record_data: dict) -> None:
        """upsert 一条错误记录到 errors 表。

        按 (fingerprint, session_id) 去重：
        - 不存在 → INSERT（occurrence_count=1）
        - 已存在 → occurrence_count += 1，刷新 last_seen/message/frames 等

        session_id 为 None 时写入 "_global"，与 errors 内存分桶逻辑一致。
        """
        _ensure_init()
        pool = _get_pool()
        conn = _get_conn()
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
            if conn is not None and not conn.closed:
                pool.putconn(conn)


# ════════════════════════════════════════════════════
#  Phase 2.4：specs 表 CRUD（独立查询，消除 N+1）—— SpecStorage ABC 实现
# ════════════════════════════════════════════════════
class PGSpecStore(SpecStorage):

    def save_spec(self, spec: dict) -> None:
        """upsert 一条 spec 到 specs 表（按 id 去重）。"""
        _ensure_init()
        pool = _get_pool()
        conn = _get_conn()
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
            if conn is not None and not conn.closed:
                pool.putconn(conn)

    def get_spec(self, spec_id: str) -> Optional[dict]:
        """从 specs 表读取一条 spec。"""
        _ensure_init()
        pool = _get_pool()
        conn = _get_conn()
        try:
            row = _query_with_retry(
                conn,
                "SELECT id, kind, target, expect, created_at, updated_at "
                "FROM specs WHERE id = %s",
                (spec_id,),
                fetch_all=False,
            )
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
            if conn is not None and not conn.closed:
                pool.putconn(conn)

    def list_specs(
        self,
        kind: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[dict]:
        """从 specs 表读取所有 spec（可按 kind/target 过滤），按 updated_at 倒序。"""
        _ensure_init()
        pool = _get_pool()
        conn = _get_conn()
        try:
            sql = (
                "SELECT id, kind, target, expect, created_at, updated_at FROM specs"
            )
            params: list = []
            conditions: list[str] = []
            if kind:
                conditions.append("kind = %s")
                params.append(kind)
            if target:
                # FIX: P2 LIKE 通配符转义 —— 用户输入含 %/_ 时被当通配符
                # 误匹配，转义后结合 ESCAPE 按字面量匹配
                escaped = (
                    target.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                conditions.append("target LIKE %s ESCAPE '\\'")
                params.append(f"%{escaped}%")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY updated_at DESC"
            rows = _query_with_retry(conn, sql, tuple(params))
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
            if conn is not None and not conn.closed:
                pool.putconn(conn)

    def delete_spec(self, spec_id: str) -> bool:
        """从 specs 表删除一条 spec，返回是否删除成功。"""
        _ensure_init()
        pool = _get_pool()
        conn = _get_conn()
        try:
            conn, rowcount = _execute_with_retry(
                conn,
                "DELETE FROM specs WHERE id = %s",
                (spec_id,),
            )
            return rowcount > 0
        finally:
            if conn is not None and not conn.closed:
                pool.putconn(conn)

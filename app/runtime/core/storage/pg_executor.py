"""PG 存储执行基础设施 —— 连接池、重连重试、熔断器、DDL 初始化、连接便捷封装。

从 pg_store.py 拆出（god object 重构）：本模块只关心"如何安全地拿到连接并执行 SQL"，
不包含任何业务表 CRUD。5 个 PG*Store 类与 errors.py 聚合层均基于本模块。

分层约定：
  - 底层 API（保留原签名，供重连测试直连调用）：
      _execute_with_retry(conn, ...) / _query_with_retry(conn, ...) —— 返回 (…, conn)，
      重连后返回最新连接，调用方负责归还。
  - 便捷 API（Store 层首选）：
      execute_sql() / query_sql() —— 自取自还连接，彻底消除各 Store 的
      try/finally putconn 样板（原 pg_store 中重复约 10 次）。
"""

import json
import time
import threading
import logging
from typing import Optional

import psycopg2
import psycopg2.pool

from app.config import settings
from app.observability import record_pg_retry
from app.runtime.core.storage._pg_errors import sanitize_pg_error
from app.runtime.core.storage.ddl import (
    DDL_TRACES,
    DDL_SESSIONS,
    DDL_ERRORS,
    DDL_SPECS,
    DDL_KB_ENTRIES,
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


def _safe_put(conn) -> None:
    """归还连接到池（关闭/空连接安全跳过）。"""
    if conn is not None and not conn.closed:
        _get_pool().putconn(conn)


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
            cur.execute(DDL_KB_ENTRIES)

            # P3-2：归档表
            if settings.pg_archive_enabled:
                cur.execute(DDL_TRACES_ARCHIVE)

            conn.commit()

            # P3-1：确保分区存在
            if settings.pg_partition_enabled:
                from app.runtime.core.storage.pg_partitions import _ensure_partitions

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
                    record_pg_retry("execute")
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

    P3-9: 与 _execute_with_retry 对齐读路径重连重试：OperationalError 时丢弃并
    获取新连接重试，避免在坏连接上重复查询。坏连接 putconn(close=True) 关闭，
    避免污染连接池。

    返回 (rows, conn)：
    - rows: fetch_all=True 返回所有行列表，fetch_all=False 返回单行
    - conn: 最新的连接对象（可能是重连后的新连接）

    FIX P3-9: 重连后调用方必须拿到最新连接归还连接池，否则新连接泄漏
    且旧连接被重复归还；旧实现只返回 rows，调用方 finally 仍归还旧 conn。
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
                    rows = cur.fetchall()
                else:
                    rows = cur.fetchone()
                return rows, conn
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
                    record_pg_retry("query")
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


# ── 便捷封装：连接生命周期由 executor 接管 ──
def execute_sql(
    sql: str,
    params: tuple = (),
    max_retries: int = 2,
    commit: bool = True,
) -> int:
    """自取自还连接执行写 SQL，返回 rowcount。

    重连后的最新连接由本函数归还（P3-9 语义内聚），调用方不再接触 conn。
    """
    conn = _get_conn()
    try:
        conn, rowcount = _execute_with_retry(conn, sql, params, max_retries, commit)
        return rowcount
    finally:
        _safe_put(conn)


def query_sql(
    sql: str,
    params: tuple = (),
    fetch_all: bool = True,
    max_retries: int = 2,
):
    """自取自还连接执行查询，返回 rows（fetch_all=False 时返回单行）。"""
    conn = _get_conn()
    try:
        rows, conn = _query_with_retry(conn, sql, params, fetch_all, max_retries)
        return rows
    finally:
        _safe_put(conn)

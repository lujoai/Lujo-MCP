"""PG traces 存储实现 —— TraceStorage ABC 的 PostgreSQL 后端。

从 pg_store.py 拆出（god object 重构）。save_entry 需要在同一连接上
做写入后的惰性分区预创建，故保留显式 _conn/_put 连接模式。
"""

import json
import time
import logging

from app.runtime.core.storage.base import TraceStorage
from app.runtime.core.storage.pg_executor import (
    _get_conn,
    _get_pool,
    _execute_with_retry,
    _query_with_retry,
    _parse_data,
    _ensure_init,
)
from app.runtime.core.storage.pg_partitions import _ensure_partitions, _archive_old_traces
from app.config import settings

logger = logging.getLogger("lujo-mcp.storage.pg")


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
            rows, conn = _query_with_retry(
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
            rows, conn = _query_with_retry(
                conn,
                "SELECT request_id FROM ("
                "  SELECT request_id, MAX(timestamp) as max_ts FROM traces GROUP BY request_id"
                ") t ORDER BY max_ts DESC LIMIT %s",
                (limit,),
            )
            return [r[0] for r in rows]
        finally:
            self._put(conn)

"""PG sessions 存储实现 —— SessionStorage ABC 的 PostgreSQL 后端。

从 pg_store.py 拆出（god object 重构）。get() 需要在同一连接上
先读后写（刷新 last_active），保留显式 _conn/_put 连接模式。
"""

import json
import time
import logging
from typing import Optional

from app.runtime.core.storage.base import SessionStorage
from app.runtime.core.storage.pg_executor import (
    _get_conn,
    _get_pool,
    _execute_with_retry,
    _query_with_retry,
    _ensure_init,
)

logger = logging.getLogger("lujo-mcp.storage.pg")


def _safe_json_loads(value) -> dict:
    """解析 metadata JSON 字段；非法 JSON（历史脏数据）降级为 {}，不抛异常穿透。

    FIX(v0.7.1-b6-4): 此前 get()/list_active() 直接 json.loads，单条脏 metadata
    即让整个会话读取抛 JSONDecodeError；现降级为 {}（会话仍可用，仅元数据丢失）。
    """
    if not isinstance(value, str):
        return value if isinstance(value, dict) else {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


class PGSessionStore(SessionStorage):
    def __init__(self):
        _ensure_init()

    def _conn(self):
        return _get_conn()

    def _put(self, conn):
        # FIX: P1-D1 —— 归还前 rollback 防止 aborted 事务连接中毒进池
        # （psycopg2 无活动事务时 rollback 为客户端空操作）
        if conn is not None and not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
            _get_pool().putconn(conn)

    def save(self, session_id: str, data: dict) -> None:
        conn = self._conn()
        try:
            # FIX(v0.7.1-b6-3): 不突变调用方 dict——last_active 用局部变量，
            # 此前 data["last_active"]=... 会把调用方传入的 dict 改掉（副作用泄漏）。
            last_active = time.time()
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
                    last_active,
                    json.dumps(data.get("metadata", {})),
                ),
            )
        finally:
            self._put(conn)

    def get(self, session_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            row, conn = _query_with_retry(
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
                "metadata": _safe_json_loads(row[3]),
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
            rows, conn = _query_with_retry(
                conn,
                "SELECT session_id, created_at, last_active, metadata FROM sessions WHERE last_active > %s",
                (cutoff,),
            )
            return [
                {
                    "session_id": r[0],
                    "created_at": r[1],
                    "last_active": r[2],
                    "metadata": _safe_json_loads(r[3]),
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

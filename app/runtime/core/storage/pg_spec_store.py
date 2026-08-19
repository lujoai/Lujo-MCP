"""PG specs 存储实现 —— SpecStorage ABC 的 PostgreSQL 后端（Phase 2.4）。

从 pg_store.py 拆出（god object 重构）：独立查询消除 N+1；
连接生命周期由 pg_executor 的 execute_sql/query_sql 接管。
"""

import json
import time
import logging
from typing import Optional

from app.runtime.core.storage.base import SpecStorage
from app.runtime.core.storage.pg_executor import (
    execute_sql,
    query_sql,
    _parse_data,
    _ensure_init,
)

logger = logging.getLogger("lujo-mcp.storage.pg")


class PGSpecStore(SpecStorage):

    def save_spec(self, spec: dict) -> None:
        """upsert 一条 spec 到 specs 表（按 id 去重）。"""
        _ensure_init()
        expect = spec.get("expect") or {}
        expect_json = json.dumps(expect, ensure_ascii=False, default=str)
        now = spec.get("updated_at") or time.time()
        execute_sql(
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

    def get_spec(self, spec_id: str) -> Optional[dict]:
        """从 specs 表读取一条 spec。"""
        _ensure_init()
        row = query_sql(
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

    def list_specs(
        self,
        kind: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[dict]:
        """从 specs 表读取所有 spec（可按 kind/target 过滤），按 updated_at 倒序。"""
        _ensure_init()
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
        rows = query_sql(sql, tuple(params))
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

    def delete_spec(self, spec_id: str) -> bool:
        """从 specs 表删除一条 spec，返回是否删除成功。"""
        _ensure_init()
        rowcount = execute_sql(
            "DELETE FROM specs WHERE id = %s",
            (spec_id,),
        )
        return rowcount > 0

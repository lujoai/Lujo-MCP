"""PostgreSQL 异步存储 —— 基于 asyncpg 连接池（Phase 3.1）。

与 pg_store.py（psycopg2 同步实现）并存，通过 feature flag
``settings.pg_async_enabled`` 切换。默认关闭，保持同步行为。

设计要点
========
- asyncpg 使用 ``$1, $2, ...`` 占位符（区别于 psycopg2 的 ``%s``）。
- JSONB 字段写入时用 ``::jsonb`` 显式转换（asyncpg 默认不自动编码 jsonb），
  读取时 jsonb 默认以 ``str`` 形式返回，统一用 ``_parse_data`` 解析。
- 连接池通过 ``asyncpg.create_pool`` 管理，min/max 从 config 读取
  （``pg_async_min`` / ``pg_async_max``，独立于 psycopg2 的连接池配置）。
- 所有方法为 async，与同步 ``TraceStorage`` / ``SessionStorage`` ABC 的同步
  契约不同。factory 在 ``pg_async_enabled=True`` 时返回本模块的类，调用链由
  ``asyncio.to_thread`` 过渡桥兜底（本任务范围外）。

方法命名与 ABC / pg_store.py 保持一致：
- TraceStorage: save_entry / get_entries / delete / cleanup_expired / list_request_ids
- SessionStorage: save / get / delete / list_active / cleanup_expired
  （任务描述中的 save_session/get_session/... 为别名描述，此处遵循 ABC 契约）

.. warning:: **ASYNC REQUIREMENT**

   本模块所有公开方法均为 ``async def``，**必须使用 ``await`` 调用**。
   同步调用（不带 ``await``）不会执行函数体，只会返回一个 coroutine 对象，
   导致数据静默丢失且不会抛出异常。Python 运行时会对此发出
   ``RuntimeWarning: coroutine was never awaited``；建议在开发和 CI 环境中
   使用 ``python -W error`` 将该警告提升为异常以尽早发现误用。
   启用本模块前请确保整条调用链已完成 async 迁移。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import asyncpg

from app.config import settings
from app.runtime.core.storage.base import TraceStorage, SessionStorage, ErrorStorage, SpecStorage
from app.runtime.core.storage._pg_errors import sanitize_pg_error

logger = logging.getLogger("ai-debug-mcp.storage.async_pg")


# ── 建表 DDL（与 pg_store.py 保持一致，确保表结构相同） ──
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

# Phase 5 P3-2：traces 归档表（结构同主表，用于冷数据归档）
DDL_TRACES_ARCHIVE = """
CREATE TABLE IF NOT EXISTS traces_archive (
    id          BIGINT,
    request_id  TEXT        NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL,
    step        TEXT        NOT NULL,
    data        JSONB,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_traces_archive_rid ON traces_archive(request_id);
CREATE INDEX IF NOT EXISTS idx_traces_archive_ts  ON traces_archive(timestamp);
"""


def _month_partition_name(year: int, month: int) -> str:
    """生成分区表名：traces_YYYY_MM"""
    return f"traces_{year:04d}_{month:02d}"


def _month_range_epoch(year: int, month: int) -> tuple[float, float]:
    """计算某月的起止 unix 时间戳（秒）。返回 (start_ts, end_ts)，区间为 [start, end)。"""
    from datetime import datetime, timezone

    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    end_dt = datetime(next_year, next_month, 1, tzinfo=timezone.utc)
    return start_dt.timestamp(), end_dt.timestamp()


# ── 全局连接池（asyncio 单线程，用 asyncio.Lock 防并发重复创建） ──
_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()
_initialized = False
_init_lock = asyncio.Lock()


async def _get_pool() -> asyncpg.Pool:
    """获取/惰性创建 asyncpg 连接池。"""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                try:
                    _pool = await asyncpg.create_pool(
                        min_size=settings.pg_async_min,
                        max_size=settings.pg_async_max,
                        host=settings.pg_host,
                        port=settings.pg_port,
                        database=settings.pg_database,
                        user=settings.pg_user,
                        password=settings.pg_password,
                    )
                    logger.info(
                        "asyncpg 连接池已创建 (min=%d, max=%d)",
                        settings.pg_async_min,
                        settings.pg_async_max,
                    )
                except (OSError, asyncpg.PostgresError) as e:
                    # 完整细节（含 DSN 参数）进日志，便于排障；传播的错误只含脱敏摘要（N4-FU-3）
                    logger.critical("asyncpg 连接失败: %s", e)
                    raise RuntimeError(sanitize_pg_error(e)) from None
    return _pool


async def close_pool() -> None:
    """优雅关闭连接池（在 lifespan shutdown 中调用）。"""
    global _pool
    async with _pool_lock:
        if _pool is not None:
            try:
                await _pool.close()
                logger.info("asyncpg 连接池已关闭")
            except Exception as e:
                logger.warning("关闭 asyncpg 连接池时出错: %s", e)
            _pool = None


def _parse_data(value):
    """安全解析 data 字段：处理 None / dict / list / JSON 字符串 / 普通字符串。"""
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


def _affected_rows(status: str) -> int:
    """解析 asyncpg execute 返回的状态串（如 'DELETE 3'）→ 影响行数。"""
    if not status:
        return 0
    parts = status.rsplit(None, 1)
    if len(parts) == 2:
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0


def _check_async_context() -> None:
    """Phase 3 防御：确保当前在 async 上下文中调用。

    同步调用 async 方法（无 await）会返回 coroutine 对象而非实际结果，
    导致数据静默丢失。此检查在调用链不正确时立即抛出明确的 RuntimeError。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError(
            "AsyncPG store 方法必须在 async 上下文中调用。"
            "当 pg_async_enabled=True 时，请确保所有调用路径已改为 async。"
            "详见 docs/internal/ROADMAP.md Phase 3 迁移指南。"
        )


async def _exec_multi(conn, sql: str) -> None:
    """执行可能包含多条语句的 DDL（按分号拆分逐条执行，确保兼容性）。

    asyncpg 的 ``execute`` 在无参数时走 simple query protocol，理论上支持多语句；
    此处按分号拆分逐条执行，最大程度兼容不同 asyncpg 版本行为。
    """
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            await conn.execute(s)


async def _create_partition_for_month(conn, year: int, month: int) -> bool:
    """为指定年月创建 traces 表的 RANGE 分区（异步版）。"""
    part_name = _month_partition_name(year, month)
    start_ts, end_ts = _month_range_epoch(year, month)

    row = await conn.fetchrow(
        "SELECT 1 FROM pg_tables WHERE tablename = $1",
        part_name,
    )
    if row is not None:
        return False

    sql = (
        f"CREATE TABLE {part_name} "
        f"PARTITION OF traces FOR VALUES FROM ({start_ts}) TO ({end_ts})"
    )
    await conn.execute(sql)
    logger.info("已创建分区: %s (%.0f ~ %.0f)", part_name, start_ts, end_ts)
    return True


async def _ensure_partitions(conn) -> int:
    """确保当月及未来 N 个月的分区存在。返回新创建的分区数量。"""
    if not settings.pg_partition_enabled:
        return 0

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    created = 0
    precreate = max(0, settings.pg_partition_precreate_months)

    for i in range(precreate + 1):
        y = now.year
        m = now.month + i
        while m > 12:
            y += 1
            m -= 12
        if await _create_partition_for_month(conn, y, m):
            created += 1

    return created


async def _archive_old_traces(conn, days: int) -> int:
    """将超过 days 天的 traces 数据归档到 traces_archive 表（异步版）。"""
    if not settings.pg_archive_enabled:
        return 0

    cutoff = time.time() - days * 86400

    if settings.pg_archive_delete_after:
        status = await conn.execute(
            "WITH moved AS ("
            "  DELETE FROM traces WHERE timestamp < $1 "
            "  RETURNING id, request_id, timestamp, step, data"
            ") "
            "INSERT INTO traces_archive (id, request_id, timestamp, step, data) "
            "SELECT id, request_id, timestamp, step, data FROM moved",
            cutoff,
        )
    else:
        status = await conn.execute(
            "INSERT INTO traces_archive (id, request_id, timestamp, step, data) "
            "SELECT id, request_id, timestamp, step, data FROM traces "
            "WHERE timestamp < $1 AND id NOT IN (SELECT id FROM traces_archive)",
            cutoff,
        )
    count = _affected_rows(status)
    if count > 0:
        logger.info("已归档 %d 条 traces 数据到 traces_archive (>%d天)", count, days)
    return count


async def _ensure_init() -> None:
    """确保表已就绪（仅初始化一次）。"""
    global _initialized
    if _initialized:
        return
    async with _init_lock:
        if _initialized:
            return
        pool = await _get_pool()
        async with pool.acquire() as conn:
            # P3-1：分区模式下用分区表替代普通表
            if settings.pg_partition_enabled:
                row = await conn.fetchrow(
                    "SELECT 1 FROM pg_tables WHERE tablename = 'traces'"
                )
                if row is None:
                    await conn.execute("""
                        CREATE TABLE traces (
                            id          BIGSERIAL,
                            request_id  TEXT        NOT NULL,
                            timestamp   DOUBLE PRECISION NOT NULL,
                            step        TEXT        NOT NULL,
                            data        JSONB,
                            PRIMARY KEY (id, timestamp)
                        ) PARTITION BY RANGE (timestamp)
                    """)
                    await conn.execute("CREATE INDEX idx_traces_rid ON traces(request_id)")
                    await conn.execute("CREATE INDEX idx_traces_ts  ON traces(timestamp)")
                    logger.info("已创建分区表 traces (RANGE BY timestamp)")
            else:
                await _exec_multi(conn, DDL_TRACES)

            await _exec_multi(conn, DDL_SESSIONS)
            await _exec_multi(conn, DDL_ERRORS)
            await _exec_multi(conn, DDL_SPECS)

            # P3-2：归档表
            if settings.pg_archive_enabled:
                await _exec_multi(conn, DDL_TRACES_ARCHIVE)

            # P3-1：确保分区存在
            if settings.pg_partition_enabled:
                await _ensure_partitions(conn)

        _initialized = True
        logger.info("asyncpg 表初始化完成")


# ════════════════════════════════════════════════════
#  Trace 存储（异步）
# ════════════════════════════════════════════════════
class AsyncPGTraceStore(TraceStorage):
    """基于 asyncpg 的异步 Trace 存储。

    .. warning:: **ASYNC REQUIREMENT** — 所有方法均为 ``async def``，必须
       ``await`` 调用。同步调用会返回 coroutine 对象而不执行函数体，
       导致数据静默丢失。详见模块级 docstring。

    注意：方法签名为 async，与同步 ``TraceStorage`` ABC 的契约不同。
    factory 在 ``pg_async_enabled=True`` 时返回本类，调用链由过渡桥兜底。
    """

    def __init__(self):
        # DDL 初始化延迟到首次方法调用（async，无法在同步 __init__ 中执行）
        pass

    async def save_entry(self, request_id: str, entry: dict) -> None:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            data = entry.get("data")
            if data is None:
                data_str = None
            elif isinstance(data, (str, int, float, bool, list, dict)):
                data_str = json.dumps(data, ensure_ascii=False, default=str)
            else:
                data_str = json.dumps(str(data), ensure_ascii=False)

            await conn.execute(
                "INSERT INTO traces (request_id, timestamp, step, data) "
                "VALUES ($1, $2, $3, $4::jsonb)",
                request_id,
                entry.get("timestamp", time.time()),
                entry.get("step", ""),
                data_str,
            )

            # P3-1：惰性检查是否需要创建新分区
            if settings.pg_partition_enabled and self._should_check_partitions():
                try:
                    await _ensure_partitions(conn)
                except Exception as e:
                    logger.warning("分区预创建失败（不影响写入）: %s", e)

    def _should_check_partitions(self) -> bool:
        """惰性分区检查：每 1000 次写入检查一次。"""
        if not hasattr(self, "_write_counter"):
            self._write_counter = 0
        self._write_counter += 1
        if self._write_counter >= 1000:
            self._write_counter = 0
            return True
        return False

    async def get_entries(self, request_id: str) -> list[dict]:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT timestamp, step, data FROM traces "
                "WHERE request_id = $1 ORDER BY timestamp",
                request_id,
            )
            return [
                {
                    "timestamp": r["timestamp"],
                    "step": r["step"],
                    "data": _parse_data(r["data"]),
                }
                for r in records
            ]

    async def delete(self, request_id: str) -> None:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM traces WHERE request_id = $1",
                request_id,
            )

    async def cleanup_expired(self, ttl_seconds: int) -> int:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            # P3-2：先执行归档
            if settings.pg_archive_enabled:
                try:
                    await _archive_old_traces(conn, settings.pg_archive_days)
                except Exception as e:
                    logger.warning("归档失败，继续执行过期清理: %s", e)

            cutoff = time.time() - ttl_seconds
            status = await conn.execute(
                "DELETE FROM traces WHERE request_id IN ("
                "  SELECT request_id FROM traces "
                "  GROUP BY request_id HAVING MAX(timestamp) < $1"
                ")",
                cutoff,
            )
            return _affected_rows(status)

    async def list_request_ids(self, limit: int = 50) -> list[str]:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT request_id FROM ("
                "  SELECT request_id, MAX(timestamp) as max_ts "
                "  FROM traces GROUP BY request_id"
                ") t ORDER BY max_ts DESC LIMIT $1",
                limit,
            )
            return [r["request_id"] for r in records]


# ════════════════════════════════════════════════════
#  Session 存储（异步）
#  方法名遵循 SessionStorage ABC 契约（与 PGSessionStore 一致）：
#  save / get / delete / list_active / cleanup_expired
# ════════════════════════════════════════════════════
class AsyncPGSessionStore(SessionStorage):
    """基于 asyncpg 的异步 Session 存储。

    .. warning:: **ASYNC REQUIREMENT** — 所有方法均为 ``async def``，必须
       ``await`` 调用。同步调用会返回 coroutine 对象而不执行函数体，
       导致数据静默丢失。详见模块级 docstring。
    """

    def __init__(self):
        pass

    async def save(self, session_id: str, data: dict) -> None:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            data["last_active"] = time.time()
            await conn.execute(
                "INSERT INTO sessions (session_id, created_at, last_active, metadata) "
                "VALUES ($1, $2, $3, $4::jsonb) "
                "ON CONFLICT (session_id) DO UPDATE SET "
                "  last_active = EXCLUDED.last_active,"
                "  metadata    = EXCLUDED.metadata",
                session_id,
                data.get("created_at", time.time()),
                data["last_active"],
                json.dumps(data.get("metadata", {}), ensure_ascii=False, default=str),
            )

    async def get(self, session_id: str) -> Optional[dict]:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT session_id, created_at, last_active, metadata "
                "FROM sessions WHERE session_id = $1",
                session_id,
            )
            if row is None:
                return None
            await conn.execute(
                "UPDATE sessions SET last_active = $1 WHERE session_id = $2",
                time.time(),
                session_id,
            )
            return {
                "session_id": row["session_id"],
                "created_at": row["created_at"],
                "last_active": row["last_active"],
                "metadata": _parse_data(row["metadata"]),
            }

    async def delete(self, session_id: str) -> None:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE session_id = $1",
                session_id,
            )

    async def list_active(self, ttl_seconds: int) -> list[dict]:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            cutoff = time.time() - ttl_seconds
            records = await conn.fetch(
                "SELECT session_id, created_at, last_active, metadata "
                "FROM sessions WHERE last_active > $1",
                cutoff,
            )
            return [
                {
                    "session_id": r["session_id"],
                    "created_at": r["created_at"],
                    "last_active": r["last_active"],
                    "metadata": _parse_data(r["metadata"]),
                }
                for r in records
            ]

    async def cleanup_expired(self, ttl_seconds: int) -> int:
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            cutoff = time.time() - ttl_seconds
            status = await conn.execute(
                "DELETE FROM sessions WHERE last_active < $1",
                cutoff,
            )
            return _affected_rows(status)


# ════════════════════════════════════════════════════
#  Phase 2.3：errors 表 CRUD（异步版）—— ErrorStorage ABC 实现
#  按 (fingerprint, session_id) upsert，冲突时 occurrence_count += 1
# ════════════════════════════════════════════════════
class AsyncPGErrorStore(ErrorStorage):
    """基于 asyncpg 的异步 Error 存储。

    .. warning:: **ASYNC REQUIREMENT** — 方法为 ``async def``，必须 ``await`` 调用。
    """

    def __init__(self):
        pass

    async def upsert_error(self, record_data: dict) -> None:
        """异步 upsert 一条错误记录到 errors 表。

        按 (fingerprint, session_id) 去重：
        - 不存在 → INSERT（occurrence_count=1）
        - 已存在 → occurrence_count += 1，刷新 last_seen/message/frames 等

        session_id 为 None 时写入 "_global"，与同步实现一致。
        """
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            frames = record_data.get("frames")
            frames_json = (
                json.dumps(frames, ensure_ascii=False, default=str)
                if frames is not None
                else None
            )
            session_id = record_data.get("session_id") or "_global"
            now = record_data.get("last_seen") or time.time()
            await conn.execute(
                """
                INSERT INTO errors
                    (error_id, fingerprint, exception_type, message, frames,
                     frame_count, traceback, source, session_id,
                     occurrence_count, first_seen, last_seen)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12)
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
            )


# ════════════════════════════════════════════════════
#  Phase 2.4：specs 表 CRUD（异步版，独立查询，消除 N+1）—— SpecStorage ABC 实现
# ════════════════════════════════════════════════════
class AsyncPGSpecStore(SpecStorage):
    """基于 asyncpg 的异步 Spec 存储。

    .. warning:: **ASYNC REQUIREMENT** — 方法为 ``async def``，必须 ``await`` 调用。
    """

    def __init__(self):
        pass

    async def save_spec(self, spec: dict) -> None:
        """异步 upsert 一条 spec 到 specs 表（按 id 去重）。"""
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            expect = spec.get("expect") or {}
            expect_json = json.dumps(expect, ensure_ascii=False, default=str)
            now = spec.get("updated_at") or time.time()
            await conn.execute(
                """
                INSERT INTO specs (id, kind, target, expect, created_at, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    kind       = EXCLUDED.kind,
                    target     = EXCLUDED.target,
                    expect     = EXCLUDED.expect,
                    updated_at = EXCLUDED.updated_at
                """,
                spec.get("id"),
                spec.get("kind", "api"),
                spec.get("target", ""),
                expect_json,
                spec.get("created_at", now),
                now,
            )

    async def get_spec(self, spec_id: str) -> Optional[dict]:
        """异步从 specs 表读取一条 spec。"""
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, kind, target, expect, created_at, updated_at "
                "FROM specs WHERE id = $1",
                spec_id,
            )
            if row is None:
                return None
            return {
                "id": row["id"],
                "kind": row["kind"],
                "target": row["target"],
                "expect": _parse_data(row["expect"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def list_specs(
        self,
        kind: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[dict]:
        """异步从 specs 表读取所有 spec（可按 kind/target 过滤），按 updated_at 倒序。"""
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            sql = "SELECT id, kind, target, expect, created_at, updated_at FROM specs"
            params: list = []
            conditions: list[str] = []
            if kind:
                conditions.append(f"kind = ${len(params) + 1}")
                params.append(kind)
            if target:
                conditions.append(f"target LIKE ${len(params) + 1}")
                params.append(f"%{target}%")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY updated_at DESC"
            records = await conn.fetch(sql, *params)
            return [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "target": r["target"],
                    "expect": _parse_data(r["expect"]),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in records
            ]

    async def delete_spec(self, spec_id: str) -> bool:
        """异步从 specs 表删除一条 spec，返回是否删除成功。"""
        _check_async_context()
        await _ensure_init()
        pool = await _get_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM specs WHERE id = $1",
                spec_id,
            )
            return _affected_rows(status) > 0

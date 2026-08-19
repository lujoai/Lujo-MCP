"""PG traces 表分区与归档管理（P3-1 / P3-2）。

从 pg_store.py 拆出（god object 重构）：只关心 traces 的月度 RANGE 分区
预创建与过期数据归档，不含连接管理（调用方传入 conn）。
"""

import time
import logging

from app.config import settings

logger = logging.getLogger("lujo-mcp.storage.pg")


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

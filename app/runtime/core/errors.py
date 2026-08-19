"""近期异常存储 —— 让全局异常钩子捕获的异常可被 MCP 工具检索。

全局异常钩子（exception_hook）捕获到的异常原本只打印到 stderr，
无法被 get_debug_context / list_recent_traces 等工具取回。
本模块用一个线程安全的有限容量双端队列，把捕获到的异常（含堆栈帧）
持久化在进程内存中，供调试工具检索。

M10 增强：指纹去重 + 聚合。相同 fingerprint（exc_type + 前3帧 file:function）
的异常累加 occurrence_count 并刷新 last_seen，避免重复错误刷屏，让 AI 看到频次。
按 proj1 架构重写（非复制 proj2 SQLite 逻辑）。
"""

import time
import uuid
import asyncio
import hashlib
import logging
import threading
from collections import deque, OrderedDict

# 最多保留最近 200 条，超出丢弃最旧的
_MAX = 200

# FIX R3-7: bucket 总数上限。_recent 按用户可控 session_id 建 bucket（每个 ≤200 条），
# bucket 数无上限时可被高频伪造 session 无界撑爆内存。LRU 淘汰最久未写入的 bucket。
_MAX_BUCKETS = 1000
_recent: OrderedDict[str, deque] = OrderedDict()
_lock = threading.Lock()

logger = logging.getLogger("lujo-mcp.errors")


def _new_id() -> str:
    return "err-" + uuid.uuid4().hex[:12]


def _get_bucket(session_id: str | None) -> str:
    return session_id or "_global"


def compute_fingerprint(exc_type: str, frames: list[dict]) -> str:
    """用异常类型 + 关键堆栈帧（file:function，忽略行号差异）算指纹。"""
    parts = [exc_type or "Unknown"]
    for f in (frames or [])[:3]:
        parts.append(f"{f.get('file', '')}:{f.get('function', '')}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _pg_upsert_error(record_data: dict) -> None:
    """同步 upsert 错误到 PG errors 表。非 PG 后端 / PG 不可用时静默跳过。

    在守护线程或 asyncio.to_thread 中调用，不阻塞主流程。

    方案 C：经存储工厂分发，不再硬编码 pg_store 模块函数。
    """
    try:
        from app.config import settings
        if settings.storage_backend != "postgresql" or settings.pg_async_enabled:
            # memory 后端 / asyncpg 后端（async 方法需在 async 上下文调用）不在此同步路径处理
            return
        from app.runtime.core.storage.factory import get_error_store
        get_error_store().upsert_error(record_data)
    except Exception:
        logger.debug("PG errors upsert 跳过", exc_info=True)


# FIX: P1-9e 同 fingerprint 节流窗口内只调度一次 PG upsert，
# 防止异常风暴下每错误无上限创建线程。
_PG_THROTTLE_SECONDS = 2.0
_last_scheduled: dict[str, float] = {}
_schedule_lock = threading.Lock()


def _schedule_pg_upsert(record_data: dict) -> None:
    """异步调度 PG upsert，不阻塞 record() 主流程。

    - 同 fingerprint 在节流窗口内只调度一次（重复错误聚合到内存层，
      PG 侧由 upsert 的 occurrence_count 逻辑收敛，不重复建线程）。
    - 有运行中的事件循环（FastAPI/uvicorn）：用 asyncio.to_thread 包装。
    - 无事件循环（同步上下文/异常钩子）：用守护线程。
    """
    fingerprint = str(record_data.get("fingerprint") or record_data.get("error_id") or "")
    with _schedule_lock:
        now = time.time()
        last = _last_scheduled.get(fingerprint, 0.0)
        if now - last < _PG_THROTTLE_SECONDS:
            # FIX: P1-9e 节流丢弃必须打日志，避免静默丢更新
            logger.debug("PG errors upsert 节流跳过 (fingerprint=%s)", fingerprint)
            return
        _last_scheduled[fingerprint] = now
        if len(_last_scheduled) > 10000:  # 防止异常种类无限增长
            _last_scheduled.clear()

    try:
        loop = asyncio.get_running_loop()
        asyncio.ensure_future(
            asyncio.to_thread(_pg_upsert_error, record_data), loop=loop
        )
    except RuntimeError:
        threading.Thread(
            target=_pg_upsert_error, args=(record_data,), daemon=True
        ).start()
    except Exception:
        logger.debug("调度 PG errors upsert 失败", exc_info=True)


def record(exc_data: dict, source: str = "unknown", session_id: str | None = None) -> str:
    """记录一条捕获到的异常，返回其 error_id。

    相同 fingerprint 的异常累加 occurrence_count 并刷新 last_seen，不新建记录。

    Phase 2.3：内存聚合后异步 upsert 到 PG errors 表（双写，内存优先读）。
    """
    frames = exc_data.get("frames", []) or []
    fingerprint = compute_fingerprint(exc_data.get("type"), frames)
    now = time.time()
    key = _get_bucket(session_id)

    pg_record = None  # 锁内捕获一致性快照，锁外异步 upsert

    with _lock:
        if key not in _recent:
            # FIX R3-7: bucket 总数达上限时 LRU 淘汰最久未写入的 bucket
            if len(_recent) >= _MAX_BUCKETS:
                oldest_key, oldest_bucket = _recent.popitem(last=False)
                logger.warning(
                    "errors bucket 数达上限(%d)，LRU 淘汰最旧 bucket %s（%d 条记录）",
                    _MAX_BUCKETS, oldest_key, len(oldest_bucket),
                )
            _recent[key] = deque(maxlen=_MAX)
        else:
            _recent.move_to_end(key)  # 维持 LRU 顺序：最近写入的 bucket 在尾
        bucket = _recent[key]

        # 从最新向最旧找同指纹记录（仅在当前桶内去重）
        for e in reversed(bucket):
            if e["fingerprint"] == fingerprint:
                e["occurrence_count"] += 1
                e["last_seen"] = now
                e["timestamp"] = now  # 向后兼容，等价于 last_seen
                e["message"] = exc_data.get("message") or e["message"]
                e["frames"] = frames or e["frames"]
                e["frame_count"] = len(e["frames"])
                e["source"] = source
                e["traceback"] = exc_data.get("traceback") or e["traceback"]
                err_id = e["error_id"]
                pg_record = {
                    "error_id": err_id,
                    "fingerprint": fingerprint,
                    "type": exc_data.get("type"),
                    "message": e["message"],
                    "frames": e["frames"],
                    "frame_count": e["frame_count"],
                    "traceback": e["traceback"],
                    "source": source,
                    "session_id": session_id,
                    "first_seen": e.get("first_seen", now),
                    "last_seen": now,
                }
                break
        else:
            err_id = _new_id()
            bucket.append({
                "error_id": err_id,
                "fingerprint": fingerprint,
                "source": source,
                "timestamp": now,
                "first_seen": now,
                "last_seen": now,
                "occurrence_count": 1,
                "type": exc_data.get("type"),
                "message": exc_data.get("message"),
                "frames": frames,
                "frame_count": len(frames),
                "traceback": exc_data.get("traceback"),
                "session_id": session_id,
            })
            pg_record = {
                "error_id": err_id,
                "fingerprint": fingerprint,
                "type": exc_data.get("type"),
                "message": exc_data.get("message"),
                "frames": frames,
                "frame_count": len(frames),
                "traceback": exc_data.get("traceback"),
                "source": source,
                "session_id": session_id,
                "first_seen": now,
                "last_seen": now,
            }

    # Phase 2.3：异步 upsert 到 PG errors 表（不阻塞主流程，PG 不可用静默跳过）
    if pg_record is not None:
        _schedule_pg_upsert(pg_record)

    # 写入/刷新异常后失效 Dashboard 概览缓存，使新数据立即可见
    # （覆盖 exception_hook 直接 record、不经过 add_log 的路径）。
    # 用惰性 import 打破 core→api 的潜在循环依赖；失败不影响记录主流程。
    try:
        from app.api.dashboard import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    return err_id


def list_recent(limit: int = 10, session_id: str | None = None) -> list:
    """按 last_seen 倒序返回最近 limit 条。"""
    with _lock:
        if session_id is not None:
            key = _get_bucket(session_id)
            items = list(_recent.get(key, []))
        else:
            items = []
            for bucket in _recent.values():
                items.extend(bucket)
    items.sort(key=lambda e: e.get("last_seen", 0), reverse=True)
    return items[:limit]


def get_latest(session_id: str | None = None) -> dict | None:
    """返回 last_seen 最大的一条。"""
    with _lock:
        if session_id is not None:
            key = _get_bucket(session_id)
            bucket = _recent.get(key)
            if not bucket:
                return None
            return max(bucket, key=lambda e: e.get("last_seen", 0))
        else:
            latest = None
            for bucket in _recent.values():
                if bucket:
                    candidate = max(bucket, key=lambda e: e.get("last_seen", 0))
                    if latest is None or candidate.get("last_seen", 0) > latest.get("last_seen", 0):
                        latest = candidate
            return latest


def get_by_id(error_id: str, session_id: str | None = None) -> dict | None:
    with _lock:
        if session_id is not None:
            key = _get_bucket(session_id)
            for e in _recent.get(key, []):
                if e["error_id"] == error_id:
                    return e
        else:
            for bucket in _recent.values():
                for e in bucket:
                    if e["error_id"] == error_id:
                        return e
    return None


def search(keyword: str, since_minutes: int = 30, session_id: str | None = None) -> list:
    """按关键字 + 时间窗（last_seen）搜索，倒序返回。"""
    keyword = (keyword or "").lower()
    cutoff = time.time() - since_minutes * 60
    with _lock:
        if session_id is not None:
            key = _get_bucket(session_id)
            items = list(_recent.get(key, []))
        else:
            items = []
            for bucket in _recent.values():
                items.extend(bucket)
    items.sort(key=lambda e: e.get("last_seen", 0), reverse=True)
    return [
        e for e in items
        if e.get("last_seen", e.get("timestamp", 0)) >= cutoff
        and (
            keyword in (e["type"] or "").lower()
            or keyword in (e["message"] or "").lower()
        )
    ]


def aggregate_by_fingerprint(session_id: str | None = None) -> list[dict]:
    """按指纹聚合统计，合并相同 fingerprint 的错误。

    返回每个指纹的聚合结果：
    - fingerprint: 错误指纹
    - type: 异常类型（取首个记录）
    - message: 错误消息（取最新记录）
    - total_occurrences: 总出现次数
    - affected_sessions: 影响的 session 数量
    - first_seen: 首次出现时间
    - last_seen: 最近出现时间
    - error_ids: 关联的 error_id 列表
    - samples: 代表性样本（最多3条）
    """
    with _lock:
        if session_id is not None:
            key = _get_bucket(session_id)
            items = list(_recent.get(key, []))
        else:
            items = []
            for bucket in _recent.values():
                items.extend(bucket)

    groups: dict[str, dict] = {}
    for item in items:
        fp = item["fingerprint"]
        if fp not in groups:
            groups[fp] = {
                "fingerprint": fp,
                "type": item.get("type"),
                "message": item.get("message"),
                "total_occurrences": 0,
                "affected_sessions": set(),
                "first_seen": item.get("first_seen", item.get("timestamp", 0)),
                "last_seen": item.get("last_seen", item.get("timestamp", 0)),
                "error_ids": [],
                "samples": [],
            }

        group = groups[fp]
        group["total_occurrences"] += item.get("occurrence_count", 1)
        bucket_key = item.get("session_id") or "_global"
        group["affected_sessions"].add(bucket_key)
        group["message"] = item.get("message") or group["message"]
        group["last_seen"] = max(group["last_seen"], item.get("last_seen", item.get("timestamp", 0)))
        if item["error_id"] not in group["error_ids"]:
            group["error_ids"].append(item["error_id"])
        if len(group["samples"]) < 3:
            group["samples"].append(item)

    for group in groups.values():
        group["affected_sessions"] = len(group["affected_sessions"])
        group["error_ids"] = group["error_ids"][:10]

    result = list(groups.values())
    result.sort(key=lambda g: g["total_occurrences"], reverse=True)
    return result


def rank_by_impact(
    session_id: str | None = None,
    since_minutes: int = 60,
) -> list[dict]:
    """按影响程度排序错误（根因排序）。

    排序权重：
    - occurrence_count (40%): 出现频次越高，影响越大
    - affected_sessions (30%): 影响的 session 越多，影响越大
    - recency (30%): 最近出现的错误权重更高

    返回排序后的错误列表，每条包含 impact_score 字段（0-100）。
    """
    aggregates = aggregate_by_fingerprint(session_id)

    if not aggregates:
        return []

    cutoff = time.time() - since_minutes * 60
    aggregates = [g for g in aggregates if g["last_seen"] >= cutoff]

    if not aggregates:
        return []

    max_occurrences = max(g["total_occurrences"] for g in aggregates)
    max_sessions = max(g["affected_sessions"] for g in aggregates)
    now = time.time()

    ranked = []
    for group in aggregates:
        occ_score = (group["total_occurrences"] / max_occurrences) * 40 if max_occurrences > 0 else 0
        sess_score = (group["affected_sessions"] / max_sessions) * 30 if max_sessions > 0 else 0
        hours_since = (now - group["last_seen"]) / 3600
        recency_score = max(0, 30 - hours_since * 5)

        impact_score = min(100, occ_score + sess_score + recency_score)

        ranked.append({
            **group,
            "impact_score": round(impact_score, 1),
            "hours_since_last_seen": round(hours_since, 1),
        })

    ranked.sort(key=lambda g: g["impact_score"], reverse=True)
    return ranked


def query_pg_errors(
    fingerprint: str | None = None,
    session_id: str | None = None,
    since_minutes: int = 1440,
    limit: int = 100,
) -> list[dict]:
    """从 PostgreSQL 查询错误记录。

    参数：
    - fingerprint: 可选，按指纹过滤
    - session_id: 可选，按 session_id 过滤
    - since_minutes: 时间范围（分钟），默认 24 小时
    - limit: 返回条数上限

    返回列表按 last_seen 倒序。PG 不可用时返回空列表。
    """
    try:
        from app.config import settings
        if settings.storage_backend != "postgresql":
            return []
        # FIX P3-10: pg_async_enabled=True 时本函数走 asyncpg 异步存储路径，
        # 不再惰性创建 psycopg2 同步池，避免双池并存。
        if settings.pg_async_enabled:
            logger.debug("query_pg_errors: pg_async_enabled=True，走 async 路径，跳过同步 psycopg2 池")
            return []
        from app.runtime.core.storage.pg_executor import _get_pool, _ensure_init, _get_conn, _parse_data
    except Exception:
        return []

    try:
        _ensure_init()
        pool = _get_pool()
        # FIX P3-10: pool.getconn() 裸获取无超时，池耗尽时永久阻塞。
        # 改用 pg_store 的 _get_conn(timeout=5.0)（有界等待，超时抛 PoolError，
        # 由下方 except Exception 兜底 return []）。psycopg2 2.9 的 getconn()
        # 本身不支持 timeout 参数，故复用既有 helper。
        conn = _get_conn(timeout=5.0)
        try:
            cur = conn.cursor()
            sql = (
                "SELECT error_id, fingerprint, exception_type, message, frames, "
                "       frame_count, traceback, source, session_id, "
                "       occurrence_count, first_seen, last_seen, created_at, updated_at "
                "FROM errors"
            )
            params: list = []
            conditions: list[str] = []

            if fingerprint:
                conditions.append("fingerprint = %s")
                params.append(fingerprint)

            if session_id:
                conditions.append("session_id = %s")
                params.append(session_id)

            if since_minutes > 0:
                cutoff = time.time() - since_minutes * 60
                conditions.append("last_seen > %s")
                params.append(cutoff)

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

            sql += " ORDER BY last_seen DESC LIMIT %s"
            params.append(min(max(limit, 1), 1000))

            cur.execute(sql, params)
            rows = cur.fetchall()

            result = []
            for row in rows:
                result.append({
                    "error_id": row[0],
                    "fingerprint": row[1],
                    "type": row[2],
                    "message": row[3],
                    "frames": _parse_data(row[4]),
                    "frame_count": row[5],
                    "traceback": row[6],
                    "source": row[7],
                    "session_id": row[8],
                    "occurrence_count": row[9],
                    "first_seen": row[10],
                    "last_seen": row[11],
                    "created_at": row[12],
                    "updated_at": row[13],
                })
            return result
        finally:
            pool.putconn(conn)
    except Exception:
        logger.debug("PG errors 查询失败", exc_info=True)
        return []

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
from collections import deque

# 最多保留最近 200 条，超出丢弃最旧的
_MAX = 200
_recent: dict[str, deque] = {}
_lock = threading.Lock()

logger = logging.getLogger("ai-debug-mcp.errors")


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
    """同步 upsert 错误到 PG errors 表。PG 不可用时静默跳过。

    在守护线程或 asyncio.to_thread 中调用，不阻塞主流程。
    """
    try:
        from app.config import settings
        if settings.storage_backend != "postgresql":
            return
        from app.mcp.core.storage.pg_store import upsert_error
        upsert_error(record_data)
    except Exception:
        logger.debug("PG errors upsert 跳过", exc_info=True)


def _schedule_pg_upsert(record_data: dict) -> None:
    """异步调度 PG upsert，不阻塞 record() 主流程。

    - 有运行中的事件循环（FastAPI/uvicorn）：用 asyncio.to_thread 包装。
    - 无事件循环（同步上下文/异常钩子）：用守护线程。
    """
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
            _recent[key] = deque(maxlen=_MAX)
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

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
import hashlib
import logging
import threading
from collections import deque, OrderedDict

from app.runtime.core.redaction import redact, redact_nested

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


def record(exc_data: dict, source: str = "unknown", session_id: str | None = None) -> str:
    """记录一条捕获到的异常，返回其 error_id。

    相同 fingerprint 的异常累加 occurrence_count 并刷新 last_seen，不新建记录。
    """
    # Storage boundary: copy and redact the complete payload so direct callers
    # cannot bypass the trace_repo boundary. Keep the caller-owned object intact.
    safe_exc_data = redact_nested(exc_data)
    safe_source = redact(source)
    frames = safe_exc_data.get("frames", []) or []
    fingerprint = compute_fingerprint(safe_exc_data.get("type"), frames)
    now = time.time()
    key = _get_bucket(session_id)

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
                e["message"] = safe_exc_data.get("message") or e["message"]
                e["frames"] = frames or e["frames"]
                e["frame_count"] = len(e["frames"])
                e["source"] = safe_source
                e["traceback"] = safe_exc_data.get("traceback") or e["traceback"]
                err_id = e["error_id"]
                break
        else:
            err_id = _new_id()
            bucket.append({
                "error_id": err_id,
                "fingerprint": fingerprint,
                "source": safe_source,
                "timestamp": now,
                "first_seen": now,
                "last_seen": now,
                "occurrence_count": 1,
                "type": safe_exc_data.get("type"),
                "message": safe_exc_data.get("message"),
                "frames": frames,
                "frame_count": len(frames),
                "traceback": safe_exc_data.get("traceback"),
                "session_id": session_id,
            })

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

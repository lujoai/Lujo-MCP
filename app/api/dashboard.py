"""Dashboard API —— Web 控制台后端接口"""

import logging
import time
import json

from fastapi import APIRouter, HTTPException

from app.mcp.core import errors, logs
from app.llm.analyzer import _get_redis_cache

logger = logging.getLogger("ai-debug-mcp.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# ── 缓存 ──
_CACHE_TTL = 30  # 秒
_cache: dict = {}  # key -> (timestamp, data)
_REDIS_CACHE_KEY = "ai-debug:dashboard:all_traces"


def invalidate_cache() -> None:
    """清除 Dashboard 概览缓存（L1 内存 + L2 Redis），使新写入的 trace 立即可见。

    由 trace 写入路径（logs.add_log 的 save_entry 路径、以及 errors.record）
    在持久化新数据后调用，避免 30s TTL 期间 Dashboard 仍展示旧数据。
    """
    _cache.pop("all_traces", None)
    redis_client = _get_redis_cache()
    if redis_client is not None:
        try:
            redis_client.delete(_REDIS_CACHE_KEY)
        except Exception:
            logger.warning("Dashboard L2 Redis 缓存清除失败", exc_info=True)


def _safe_int(value, default=0):
    """安全转换为 int"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_error_summary(err: dict) -> dict:
    """从 errors 缓冲的记录中提取摘要"""
    trace_id = err.get("error_id", "")
    meta = {}
    try:
        for entry in logs.get_logs(trace_id):
            if entry.get("step") == "trace_meta":
                data = entry.get("data")
                if isinstance(data, dict):
                    meta = data
                    break
    except Exception:
        pass

    trace_kind = meta.get("trace_kind", "exception")
    verify_count = 0
    has_silent_failure = False
    try:
        for entry in logs.get_logs(trace_id):
            if entry.get("step") == "verify":
                data = entry.get("data")
                if isinstance(data, dict):
                    verify_count += 1
                    if data.get("silent_failure"):
                        has_silent_failure = True
    except Exception:
        pass

    return {
        "trace_id": trace_id,
        "timestamp": err.get("timestamp", 0),
        "type": err.get("type", "ERROR"),
        "message": (err.get("message") or "")[:200],
        "trace_kind": trace_kind,
        "occurrence_count": err.get("occurrence_count", 1),
        "has_silent_failure": has_silent_failure,
        "verify_count": verify_count,
    }


def _extract_trace_summary(request_id: str) -> dict | None:
    """从存储中提取 trace 摘要信息"""
    entries = logs.get_logs(request_id)
    if not entries:
        return None

    summary = {
        "trace_id": request_id,
        "timestamp": 0,
        "type": "debug",
        "message": "",
        "trace_kind": "debug",
        "occurrence_count": 1,
        "has_silent_failure": False,
        "verify_count": 0,
    }

    spec_diffs = []
    for entry in entries:
        ts = entry.get("timestamp", 0)
        if ts > summary["timestamp"]:
            summary["timestamp"] = ts

        step = entry.get("step", "")
        data = entry.get("data")

        if isinstance(data, str):
            if step == "error":
                summary["type"] = "ERROR"
                summary["message"] = data[:200]
                summary["trace_kind"] = "exception"
            elif step == "request_start":
                summary["message"] = data[:200]
            elif step == "response_ready":
                summary["type"] = "RESPONSE"
            continue

        if not isinstance(data, dict):
            continue

        if step == "request_start":
            summary["type"] = data.get("method", "REQUEST")
            summary["message"] = data.get("url", "")[:200]
        elif step == "response_ready":
            status = _safe_int(data.get("status", 0))
            summary["type"] = f"RESPONSE {status}"
            if status >= 400:
                summary["trace_kind"] = "exception"
        elif step == "error":
            summary["type"] = data.get("error_type", "ERROR")
            summary["message"] = (data.get("message", "") or "")[:200]
            summary["trace_kind"] = "exception"
            summary["has_silent_failure"] = data.get("silent", False)
        elif step == "verify":
            spec_diffs.append(data)
            if data.get("silent_failure"):
                summary["has_silent_failure"] = True

    summary["verify_count"] = len(spec_diffs)
    return summary


def _collect_all_traces(limit: int = 100) -> list[dict]:
    """合并 errors 缓冲和 TraceStorage 中的 trace 摘要（带多级缓存 L1+L2）"""
    limit = min(max(limit, 1), 1000)

    now = time.monotonic()

    # ── L1: 内存缓存 ──
    cached = _cache.get("all_traces")
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1][:limit]

    # ── L2: Redis 缓存 ──
    redis_client = _get_redis_cache()
    if redis_client is not None:
        try:
            raw = redis_client.get(_REDIS_CACHE_KEY)
            if raw:
                result = json.loads(raw)
                # L2 命中 → 回填 L1
                _cache["all_traces"] = (now, result)
                return result[:limit]
        except Exception:
            logger.warning("Dashboard L2 Redis 缓存读取失败", exc_info=True)

    # ── 计算 ──
    result = []
    seen_ids = set()

    for err in errors.list_recent(limit=limit):
        summary = _extract_error_summary(err)
        if summary and summary["trace_id"] not in seen_ids:
            result.append(summary)
            seen_ids.add(summary["trace_id"])

    for rid in logs.list_request_ids(limit=limit):
        if rid not in seen_ids:
            summary = _extract_trace_summary(rid)
            if summary:
                result.append(summary)
                seen_ids.add(rid)

    result.sort(key=lambda t: t.get("timestamp", 0), reverse=True)

    # ── 写 L1 + L2 ──
    _cache["all_traces"] = (now, result)
    if redis_client is not None:
        try:
            redis_client.setex(
                _REDIS_CACHE_KEY,
                _CACHE_TTL,
                json.dumps(result, ensure_ascii=False, default=str),
            )
        except Exception:
            logger.warning("Dashboard L2 Redis 缓存写入失败", exc_info=True)

    return result[:limit]


@router.get("/stats")
def get_stats():
    """控制台概览统计"""
    all_traces = _collect_all_traces(limit=100)

    total = len(all_traces)
    silent_count = sum(1 for t in all_traces if t.get("has_silent_failure"))
    exception_count = sum(
        1 for t in all_traces
        if t.get("trace_kind") == "exception" and not t.get("has_silent_failure")
    )

    from app.mcp.verifier import spec_store
    spec_count = len(spec_store.list_specs())

    return {
        "total_traces": total,
        "silent_failures": silent_count,
        "exceptions": exception_count,
        "spec_count": spec_count,
    }


@router.get("/traces")
def list_traces(limit: int = 100):
    """列出最近 traces（含 verify 结果摘要），limit 上限 1000"""
    limit = min(max(limit, 1), 1000)
    result = _collect_all_traces(limit=limit)
    return {"traces": result, "total": len(result)}


@router.get("/trace/{trace_id}")
def get_trace_detail(trace_id: str):
    """获取 trace 详情（含 spec_diffs）"""
    from app.mcp.builders.context import build_debug_context

    ctx = build_debug_context(trace_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"找不到 trace {trace_id}")

    # 精简返回（去掉 runtime/network_trace 等大字段）
    return {
        "trace_id": ctx.get("trace_id"),
        "trace_kind": ctx.get("trace_kind"),
        "exception": ctx.get("exception"),
        "errors": ctx.get("errors"),
        "spec_diffs": ctx.get("spec_diffs"),
        "code_snippets": ctx.get("code_snippets"),
        "source": ctx.get("source"),
        "extra": ctx.get("extra"),
    }


@router.get("/specs")
def list_specs():
    """列出所有已存规范"""
    from app.mcp.verifier import spec_store
    return {"specs": spec_store.list_specs()}


# ── 智能错误分析引擎端点 ──

@router.get("/errors/aggregated")
def get_errors_aggregated(session_id: str | None = None):
    """按指纹聚合统计错误（智能分析引擎 Phase 7）"""
    aggregated = errors.aggregate_by_fingerprint(session_id=session_id)
    return {
        "aggregates": aggregated,
        "total_fingerprints": len(aggregated),
        "total_occurrences": sum(g["total_occurrences"] for g in aggregated),
    }


@router.get("/errors/ranked")
def get_errors_ranked(
    session_id: str | None = None,
    since_minutes: int = 60,
    limit: int = 20,
):
    """按影响程度排序错误（根因排序，智能分析引擎 Phase 7）"""
    since_minutes = max(since_minutes, 1)
    limit = min(max(limit, 1), 100)

    ranked = errors.rank_by_impact(
        session_id=session_id,
        since_minutes=since_minutes,
    )[:limit]

    return {
        "ranked_errors": ranked,
        "total": len(ranked),
        "since_minutes": since_minutes,
    }


@router.get("/errors/history")
def get_errors_history(
    fingerprint: str | None = None,
    session_id: str | None = None,
    since_minutes: int = 1440,
    limit: int = 100,
):
    """查询错误历史记录（智能分析引擎 Phase 7）

    优先从 PostgreSQL 查询（长期历史），PG 不可用时返回空列表。
    """
    since_minutes = max(since_minutes, 1)
    limit = min(max(limit, 1), 1000)

    history = errors.query_pg_errors(
        fingerprint=fingerprint,
        session_id=session_id,
        since_minutes=since_minutes,
        limit=limit,
    )

    return {
        "errors": history,
        "total": len(history),
        "since_minutes": since_minutes,
    }

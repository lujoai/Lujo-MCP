"""Dashboard API —— Web 控制台后端接口"""

import logging
import time
import json
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.rbac import require_role
from app.runtime.core import errors, logs
from app.llm.cache import _get_redis_cache

logger = logging.getLogger("lujo-mcp.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# ── 缓存 ──
_CACHE_TTL = 30  # 秒
_cache: dict = {}  # key -> (timestamp, data)
_REDIS_CACHE_KEY = "ai-debug:dashboard:all_traces"


def invalidate_cache(source: str | None = None) -> None:
    """清除 Dashboard 概览缓存（L1 内存 + L2 Redis），使新写入的 trace 立即可见。

    由 trace 写入路径（logs.add_log 的 save_entry 路径、以及 errors.record）
    在持久化新数据后调用，避免 30s TTL 期间 Dashboard 仍展示旧数据。

    DASH-SSE-001：同时广播 SSE 变更信号，订阅了 /api/dashboard/stream 的前端
    收到后即时 re-fetch（叠加在轮询之上）。无订阅者或功能关闭时为 no-op。
    """
    _cache.pop("all_traces", None)
    redis_client = _get_redis_cache()
    if redis_client is not None:
        try:
            redis_client.delete(_REDIS_CACHE_KEY)
        except Exception:
            logger.warning("Dashboard L2 Redis 缓存清除失败", exc_info=True)

    # SSE 实时推送：通知 Dashboard 客户端数据已变更
    try:
        from app.api.dashboard_events import broadcast_dashboard_event
        broadcast_dashboard_event({
            "type": "dashboard_changed",
            "source": source,
            "ts": time.time(),
        })
    except Exception:
        pass


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
    """合并 errors 缓冲和 TraceStorage 中的 trace 摘要（带多级缓存 L1+L2）

    FIX: P1-E1 —— 缓存按最大 limit（1000）计算并缓存完整结果，调用方按需切片。
    此前按首个请求的 limit 计算并缓存整个 result：30s TTL 窗口内小 limit 请求
    （如 10）先执行并缓存 10 条，后续大 limit 请求（如 1000）命中缓存
    `cached[1][:1000]` 却只有 10 条——返回被首个请求截断的数据（L2 Redis
    跨实例共享，污染面更大）。
    """
    limit = min(max(limit, 1), 1000)
    # 缓存计算/存储统一用最大档（与 limit 的钳制上限一致），保证缓存结果
    # 对任意后续请求都足够长
    cache_limit = 1000

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

    # ── 计算（按 cache_limit，而非调用方 limit）──
    result = []
    seen_ids = set()

    for err in errors.list_recent(limit=cache_limit):
        summary = _extract_error_summary(err)
        if summary and summary["trace_id"] not in seen_ids:
            result.append(summary)
            seen_ids.add(summary["trace_id"])

    for rid in logs.list_request_ids(limit=cache_limit):
        if rid not in seen_ids:
            summary = _extract_trace_summary(rid)
            if summary:
                result.append(summary)
                seen_ids.add(rid)

    result.sort(key=lambda t: t.get("timestamp", 0), reverse=True)

    # ── 写 L1 + L2（完整 cache_limit 长度）──
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


@router.get("/stats", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def get_stats():
    """控制台概览统计"""
    all_traces = _collect_all_traces(limit=100)

    total = len(all_traces)
    silent_count = sum(1 for t in all_traces if t.get("has_silent_failure"))
    exception_count = sum(
        1 for t in all_traces
        if t.get("trace_kind") == "exception" and not t.get("has_silent_failure")
    )

    from app.runtime.verifier import spec_store
    spec_count = len(spec_store.list_specs())

    return {
        "total_traces": total,
        "silent_failures": silent_count,
        "exceptions": exception_count,
        "spec_count": spec_count,
    }


@router.get("/traces", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def list_traces(limit: int = 100):
    """列出最近 traces（含 verify 结果摘要），limit 上限 1000"""
    limit = min(max(limit, 1), 1000)
    result = _collect_all_traces(limit=limit)
    return {"traces": result, "total": len(result)}


@router.get("/trace/{trace_id}", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def get_trace_detail(trace_id: str):
    """获取 trace 详情（含 spec_diffs + quality_report，v0.4.0）"""
    from app.runtime.context.builder import build_debug_context

    ctx = build_debug_context(trace_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"找不到 trace {trace_id}")

    # v0.4.0: 注入质量报告（纯函数评分，不触发 LLM 调用）
    ctx_dict = ctx.model_dump()
    quality_report = _safe_build_quality_report(ctx_dict)

    # 精简返回（去掉 runtime/network_trace 等大字段）
    return {
        "trace_id": ctx_dict.get("trace_id"),
        "trace_kind": ctx_dict.get("trace_kind"),
        "exception": ctx_dict.get("exception"),
        "errors": ctx_dict.get("errors"),
        "spec_diffs": ctx_dict.get("spec_diffs"),
        "code_snippets": ctx_dict.get("code_snippets"),
        "source": ctx_dict.get("source"),
        "extra": ctx_dict.get("extra"),
        "quality_report": quality_report,
    }


def _safe_build_quality_report(debug_context: dict) -> dict | None:
    """对 build_debug_context 的结果进行质量评分（v0.4.0）。

    Dashboard 场景不调用 RepairContextAssembler（避免触发 LLM/向量召回等重负载），
    直接用 QualityScorer 纯函数对 debug_context 评分。
    评分失败或 feature flag 关闭时返回 None，向后兼容。
    """
    try:
        from app.quality.scorer import evaluate, is_enabled

        if not is_enabled():
            return None

        # Dashboard 无 repair_context，传入空 dict（scorer 内部各维度评分函数均做兜底）
        agent_ctx = {
            "debug_context": debug_context,
            "repair_context": {},
        }
        report = evaluate(agent_ctx)
        return report.model_dump()
    except Exception:
        logger.warning("Dashboard 质量评分失败", exc_info=True)
        return None


@router.get("/trace/{trace_id}/quality", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def get_trace_quality(trace_id: str):
    """单独获取 trace 的质量报告（v0.4.0）。

    复用 get_trace_detail 的评分逻辑，但只返回 quality_report 字段，
    供前端单独轮询或刷新质量评分（避免拉取完整 trace 详情）。
    """
    from app.runtime.context.builder import build_debug_context

    ctx = build_debug_context(trace_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"找不到 trace {trace_id}")

    quality_report = _safe_build_quality_report(ctx.model_dump())
    if quality_report is None:
        return {"trace_id": trace_id, "quality_report": None}

    return {"trace_id": trace_id, "quality_report": quality_report}


@router.get("/specs", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def list_specs():
    """列出所有已存规范"""
    from app.runtime.verifier import spec_store
    return {"specs": spec_store.list_specs()}


# ── 实时 SSE 推送（DASH-SSE-001）──

@router.get("/stream", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
async def dashboard_stream(request: Request):
    """Dashboard 实时 SSE 推送通道。

    客户端用 ``EventSource`` 订阅；服务端在 trace/error 写入时（``invalidate_cache``
    钩子）广播 ``dashboard_changed`` 信号，客户端收到后即时 re-fetch stats+traces，
    替代"等下一个 10s 轮询周期"。

    鉴权：``EventSource`` 无法设置自定义 header，前端先用 header 换取短时
    beacon 令牌（``POST /auth/beacon-token``）再以 ``?token=`` 携带（S1，
    避免永久 API Key 进查询参数被明文记录）。

    降级：``dashboard_sse_enabled=False`` 时返回 503（功能未启用）。
    """
    from app.api.dashboard_events import dashboard_hub
    from app.config import settings

    if not settings.dashboard_sse_enabled:
        return JSONResponse({"detail": "Dashboard SSE 未启用"}, status_code=503)

    try:
        q = dashboard_hub.subscribe()
    except PermissionError:
        # FIX: P3-7 订阅数已达上限，拒绝新长连接
        return JSONResponse({"detail": "Dashboard SSE 订阅数达上限"}, status_code=429)

    async def event_stream():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    # 15s 无事件则发心跳，防止反向代理超时断开长连接
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if dashboard_hub.is_close_event(msg):
                    break
                yield dashboard_hub.format_event(msg)
        except asyncio.CancelledError:
            pass
        finally:
            dashboard_hub.unsubscribe(q)

    resp = StreamingResponse(event_stream(), media_type="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # nginx 透传：禁缓冲，保实时性
    return resp


# ── 智能错误分析引擎端点 ──

@router.get("/errors/aggregated", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def get_errors_aggregated(session_id: str | None = None):
    """按指纹聚合统计错误（智能分析引擎 Phase 7）"""
    aggregated = errors.aggregate_by_fingerprint(session_id=session_id)
    return {
        "aggregates": aggregated,
        "total_fingerprints": len(aggregated),
        "total_occurrences": sum(g["total_occurrences"] for g in aggregated),
    }


@router.get("/errors/ranked", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
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


@router.get("/errors/history", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
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

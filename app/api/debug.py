"""调试相关 API 路由"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import json

from app.config import settings
from app.runtime.core.logs import create_request_id, add_log, get_logs
from app.runtime.core.session import session_manager
from app.runtime.context.builder import build_context
from app.runtime.collectors.runtime import collect_runtime_snapshot
from app.runtime.collectors.stacktrace import capture_exception
from app.llm.analyzer import analyze, analyze_stream_async
from app.llm.analysis_queue import get_analysis_queue, QueueFullError
from app.agent.repair_queue import get_repair_queue, QueueFullError as RepairQueueFullError
from app.schemas import (
    DebugRequest, AnalyzeRequest, DebugResponse, VerifyRequest, VerifyUiRequest,
    SourcemapUploadRequest,
)
from app.auth.rbac import require_role

logger = logging.getLogger("lujo-mcp.api")

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.post("/run", dependencies=[Depends(require_role("admin", "developer"))])
def debug_run(req: DebugRequest) -> DebugResponse:
    """执行调试流程：记录请求 → 处理 → 构建上下文"""
    request_id = create_request_id()

    try:
        add_log(request_id, "request_start", req.payload)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    error_info = None
    try:
        add_log(request_id, "processing", {"metadata": req.metadata})
        result = {"echo": req.payload, "status": "success"}
        add_log(request_id, "response_ready", result)
    except Exception as e:
        error_info = capture_exception(e)
        try:
            # 把完整异常（含堆栈帧）写入 trace，使 context/analyze 可检索
            add_log(request_id, "error", error_info)
        except Exception as log_error:
            logger.error(str(log_error), exc_info=True)
        logger.error(str(e), exc_info=True)
        result = {"status": "error", "message": "Internal server error"}

    try:
        trace = get_logs(request_id)
        context = build_context(request_id, trace)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if error_info:
        try:
            # 保留完整异常（含 frames），供 LLM 分析与源码片段使用
            context["exception"] = error_info
            # FR11：附加报错行源码片段
            if error_info.get("frames"):
                from app.runtime.collectors.code_locator import get_snippets_for_frames
                context["code_snippets"] = [
                    s.model_dump() for s in get_snippets_for_frames(error_info["frames"])
                ]
        except Exception:
            context["exception"] = {"type": "Unknown", "message": str(error_info)}

    return DebugResponse(
        request_id=request_id,
        result=result,
        trace=trace,
        context=context,
    )


@router.post("/analyze", dependencies=[Depends(require_role("admin", "developer"))])
def debug_analyze(req: AnalyzeRequest):
    """对指定请求进行 LLM 分析"""
    try:
        trace = get_logs(req.request_id)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not trace:
        raise HTTPException(status_code=404, detail=f"找不到请求 {req.request_id}")

    try:
        context = build_context(req.request_id, trace)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    # 若 errors 中含堆栈帧，提升到 exception（供 LLM 分析）
    for err in context.get("errors", []):
        if isinstance(err, dict) and err.get("frames"):
            context["exception"] = err
            break

    try:
        context["runtime"] = collect_runtime_snapshot()
    except Exception as e:
        logger.error(str(e), exc_info=True)
        context["runtime"] = {"error": "Tool execution failed"}

    try:
        analysis = analyze(context)
        return {
            "request_id": req.request_id,
            "context": context,
            "analysis": analysis,
        }
    except RuntimeError as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=502, detail="Internal server error")
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/analyze/stream", dependencies=[Depends(require_role("admin", "developer"))])
async def debug_analyze_stream(req: AnalyzeRequest):
    """流式 LLM 分析（SSE）"""
    try:
        trace = get_logs(req.request_id)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not trace:
        raise HTTPException(status_code=404, detail=f"找不到请求 {req.request_id}")

    context = build_context(req.request_id, trace)
    try:
        context["runtime"] = collect_runtime_snapshot()
    except Exception:
        context["runtime"] = {}

    async def event_stream():
        try:
            # Phase 3.2：直接使用异步流式分析，原生 async for 迭代，
            # 无需 to_thread 包装同步生成器。
            async for chunk in analyze_stream_async(context):
                data = json.dumps({"chunk": chunk}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(str(e), exc_info=True)
            yield f"data: {json.dumps({'error': 'Tool execution failed'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/analyze/async", dependencies=[Depends(require_role("admin", "developer"))])
async def debug_analyze_async(req: AnalyzeRequest):
    """异步 LLM 分析（P3-6 削峰队列）。

    走有界队列 + K 常驻消费协程，对齐 LLM RPM/TPM；
    返回 job_id，客户端轮询 ``/api/debug/analyze/result/{job_id}`` 取结果。
    """
    if not settings.llm_async_analysis_enabled:
        raise HTTPException(status_code=501, detail="async analysis disabled")

    try:
        trace = get_logs(req.request_id)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not trace:
        raise HTTPException(status_code=404, detail=f"找不到请求 {req.request_id}")

    try:
        context = build_context(req.request_id, trace)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    # 与 /analyze 保持一致：errors 中含堆栈帧则提升到 exception
    for err in context.get("errors", []):
        if isinstance(err, dict) and err.get("frames"):
            context["exception"] = err
            break

    try:
        context["runtime"] = collect_runtime_snapshot()
    except Exception as e:
        logger.error(str(e), exc_info=True)
        context["runtime"] = {"error": "Tool execution failed"}

    try:
        job_id = await get_analysis_queue().enqueue(context, model=None)
    except QueueFullError:
        return JSONResponse(
            status_code=429,
            content={
                "error": "queue_full",
                "queue_size": get_analysis_queue().queue_size(),
            },
        )

    return {"job_id": job_id, "status": "queued"}


@router.get("/analyze/result/{job_id}", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def debug_analyze_result(job_id: str):
    """查询异步分析任务状态/结果。"""
    if not settings.llm_async_analysis_enabled:
        raise HTTPException(status_code=501, detail="async analysis disabled")

    job = get_analysis_queue().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"找不到任务 {job_id}")
    return job


@router.post("/repair/async", dependencies=[Depends(require_role("admin", "developer"))])
async def debug_repair_async(req: AnalyzeRequest):
    """异步生成可执行修复方案（AI Debug Agent Phase 1）。

    走有界队列 + K 常驻消费协程；返回 job_id，
    客户端轮询 ``/api/debug/repair/result/{job_id}`` 取结果。
    需 settings.agent_enabled=True，否则返回 501。
    """
    if not settings.agent_enabled:
        raise HTTPException(status_code=501, detail="agent disabled")

    try:
        trace = get_logs(req.request_id)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not trace:
        raise HTTPException(status_code=404, detail=f"找不到请求 {req.request_id}")

    try:
        context = build_context(req.request_id, trace)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    # 与 /analyze 保持一致：errors 中含堆栈帧则提升到 exception
    for err in context.get("errors", []):
        if isinstance(err, dict) and err.get("frames"):
            context["exception"] = err
            break

    try:
        context["runtime"] = collect_runtime_snapshot()
    except Exception as e:
        logger.error(str(e), exc_info=True)
        context["runtime"] = {"error": "Tool execution failed"}

    try:
        job_id = await get_repair_queue().enqueue(context, model=None)
    except RepairQueueFullError:
        return JSONResponse(
            status_code=429,
            content={
                "error": "queue_full",
                "queue_size": get_repair_queue().queue_size(),
            },
        )

    return {"job_id": job_id, "status": "queued"}


@router.get("/repair/result/{job_id}", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def debug_repair_result(job_id: str):
    """查询异步修复任务状态/结果。结构对称 /analyze/result/{job_id}。"""
    if not settings.agent_enabled:
        raise HTTPException(status_code=501, detail="agent disabled")

    job = get_repair_queue().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"找不到任务 {job_id}")
    return job


@router.get("/runtime", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def get_runtime():
    """获取当前运行时快照"""
    try:
        return collect_runtime_snapshot()
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/session", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def list_sessions():
    """列出活跃的调试会话"""
    try:
        sessions = session_manager.list_active()
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    return {
        "count": len(sessions),
        "sessions": [
            {
                "session_id": s["session_id"],
                "created_at": s["created_at"],
                "last_active": s["last_active"],
                "idle_seconds": round(time.time() - s.get("last_active", time.time()), 1),
                "metadata": s.get("metadata", {}),
            }
            for s in sessions
        ],
    }


@router.post("/verify", dependencies=[Depends(require_role("admin", "developer"))])
def debug_verify(req: VerifyRequest):
    """比对实际结果 vs 期望规范，自动检测静默失败。

    请求体：
      actual: dict         — 实际结果（status_code、body、error 等）
      spec?: dict          — 期望规范（与 spec_id 二选一）
      spec_id?: str        — 已存储规范的 ID
      trace_id?: str       — 关联的 trace_id
    """
    from app.mcp.tools.verify_api import verify_handler

    try:
        return verify_handler(req.model_dump())
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/verify/ui", dependencies=[Depends(require_role("admin", "developer"))])
def debug_verify_ui(req: VerifyUiRequest):
    """按 UI 规范启动 Playwright 自动遍历页面并验证交互结果（FR14）。

    请求体：
      spec?: dict          — UI 规范 {kind:'ui', target, expect:{interactions:[...]}}
      spec_id?: str        — 已存储规范的 ID
      timeout_ms?: int     — 单个操作超时毫秒
    """
    from app.mcp.tools.verify_ui_api import verify_ui_handler

    try:
        return verify_ui_handler(req.model_dump())
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/health", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def debug_health():
    """健康检查接口，供 XHR 测试使用"""
    return {"status": "ok", "timestamp": time.time()}


@router.post("/sourcemap", dependencies=[Depends(require_role("admin", "developer"))])
def debug_upload_sourcemap(req: SourcemapUploadRequest):
    """上传 Source Map（v0.5.1）：用于把前端 minified 堆栈还原为原始源码。

    请求体：
      artifact: str   — JS 产物标识（如 "app.9f3b2c.js"，解析时也按帧文件 basename 匹配）
      map: dict       — 完整 source map JSON 对象（至少含 mappings/sources）
      release?: str   — 可选发布标识（仅透传回执，便于对账）

    存储为进程内 TTL + LRU 容量限制（单机；不落 PG）。
    """
    if not settings.sourcemap_enabled:
        raise HTTPException(status_code=503, detail="sourcemap resolution disabled (SOURCEMAP_ENABLED=false)")

    from app.runtime.collectors.sourcemap_store import upload_sourcemap

    try:
        receipt = upload_sourcemap(req.artifact, req.map)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    if req.release:
        receipt["release"] = req.release
    return receipt


@router.post("/echo", dependencies=[Depends(require_role("admin"))])
def debug_echo(body: dict):
    """回显接口，返回请求体"""
    if not settings.debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    return {"status": "ok", "received": body}


@router.get("/token", dependencies=[Depends(require_role("admin"))])
def debug_token():
    """返回带 token 的响应，用于测试响应脱敏"""
    if not settings.debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    return {"token": "abc123", "user_id": 123, "username": "admin"}

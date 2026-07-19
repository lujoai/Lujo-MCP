"""调试相关 API 路由"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
import json

from app.mcp.core.logs import create_request_id, add_log, get_logs
from app.mcp.core.session import session_manager
import time
from app.mcp.builders.context import build_context
from app.mcp.collectors.runtime import collect_runtime_snapshot
from app.mcp.collectors.stacktrace import capture_exception
from app.llm.analyzer import analyze, analyze_stream
from app.schemas import DebugRequest, AnalyzeRequest, DebugResponse, DebugContext

logger = logging.getLogger("ai-debug-mcp.api")

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.post("/run")
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
                from app.mcp.collectors.code_locator import get_snippets_for_frames
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


@router.post("/analyze")
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


@router.post("/analyze/stream")
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
            for chunk in analyze_stream(context):
                data = json.dumps({"chunk": chunk}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(str(e), exc_info=True)
            yield f"data: {json.dumps({'error': 'Tool execution failed'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/runtime")
def get_runtime():
    """获取当前运行时快照"""
    try:
        return collect_runtime_snapshot()
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/session")
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


@router.post("/verify")
def debug_verify(body: dict):
    """比对实际结果 vs 期望规范，自动检测静默失败。

    请求体：
      actual: dict         — 实际结果（status_code、body、error 等）
      spec?: dict          — 期望规范（与 spec_id 二选一）
      spec_id?: str        — 已存储规范的 ID
      trace_id?: str       — 关联的 trace_id
    """
    from app.mcp.tools.verify_api import verify_handler

    try:
        return verify_handler(body)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/verify/ui")
def debug_verify_ui(body: dict):
    """按 UI 规范启动 Playwright 自动遍历页面并验证交互结果（FR14）。

    请求体：
      spec?: dict          — UI 规范 {kind:'ui', target, expect:{interactions:[...]}}
      spec_id?: str        — 已存储规范的 ID
      timeout_ms?: int     — 单个操作超时毫秒
    """
    from app.mcp.tools.verify_ui_api import verify_ui_handler

    try:
        return verify_ui_handler(body)
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/health")
def debug_health():
    """健康检查接口，供 XHR 测试使用"""
    return {"status": "ok", "timestamp": time.time()}


@router.post("/echo")
def debug_echo(body: dict):
    """回显接口，返回请求体"""
    return {"status": "ok", "received": body}


@router.get("/token")
def debug_token():
    """返回带 token 的响应，用于测试响应脱敏"""
    return {"token": "abc123", "user_id": 123, "username": "admin"}

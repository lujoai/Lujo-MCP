"""
/ingest/* —— 外部服务 / 浏览器 SDK 原始接入端点。

与 /mcp/tools 不同，这里直接接收业务数据结构，便于前端 SDK 和非 Python 服务上报。
鉴权由 AuthMiddleware 统一兜底（fail-closed + 恒定时间比较），不在此重复实现，
保持 proj1 安全中间件体系不变。
"""
import logging

from fastapi import APIRouter, HTTPException

from app.mcp.tools.network_api import tool_ingest_network, tool_get_network_trace
from app.mcp.tools.silent_failure_api import tool_ingest_silent_failure
from app.mcp.tools.ingest_api import tool_ingest_error
from app.mcp.tools.console_api import tool_ingest_console
from app.mcp.core.trace_repo import save_ui_event

router = APIRouter(prefix="/ingest", tags=["ingest"])
logger = logging.getLogger("ai-debug-mcp.ingest")


@router.post("/network")
def ingest_network(req: dict):
    """单条网络请求记录上报。"""
    try:
        return tool_ingest_network(
            record=req.get("record", {}),
            trace_id=req.get("trace_id"),
            request_id=req.get("request_id"),
        )
    except ValueError as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=422, detail="Invalid request payload")
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=400, detail="Internal server error")


@router.get("/network/{trace_id}")
def get_network_trace(trace_id: str):
    """查询某 trace 关联的网络请求记录。"""
    return tool_get_network_trace(trace_id)


@router.post("/silent-failure")
def ingest_silent_failure(req: dict):
    """浏览器 SDK 上报静默失败。"""
    try:
        return tool_ingest_silent_failure(
            message=req.get("message", ""),
            frames=req.get("frames"),
            ui_events=req.get("ui_events"),
            network_records=req.get("network_records"),
            expectation=req.get("expectation"),
            observed=req.get("observed"),
            observed_events=req.get("observed_events"),
            source=req.get("source", "browser_sdk"),
            extra=req.get("extra"),
            trace_id=req.get("trace_id"),
            session_id=req.get("session_id"),
        )
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=400, detail="Internal server error")


@router.post("/error")
def ingest_error(req: dict):
    """外部服务主动上报异常，复用 ingest_error 工具逻辑。"""
    try:
        return tool_ingest_error(
            exc_type=req.get("exc_type", "UnknownError"),
            message=req.get("message", ""),
            frames=req.get("frames", []),
            source=req.get("source", "http_ingest"),
            extra=req.get("extra"),
            trace_id=req.get("trace_id"),
            session_id=req.get("session_id"),
        )
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=400, detail="Internal server error")


@router.post("/console")
def ingest_console(req: dict):
    """浏览器 SDK 上报控制台日志，复用 ingest_console 工具逻辑。"""
    try:
        return tool_ingest_console(
            level=req.get("level", "info"),
            message=req.get("message", ""),
            source=req.get("source", "browser_sdk"),
            extra=req.get("extra"),
            trace_id=req.get("trace_id"),
            request_id=req.get("request_id"),
        )
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=400, detail="Internal server error")


@router.post("/ui-event")
def ingest_ui_event(req: dict):
    """浏览器 SDK 上报 UI 事件，复用 save_ui_event 落库。"""
    try:
        trace_id = req.get("trace_id")
        event_id = save_ui_event(
            event=req.get("event", {}) or {},
            trace_id=trace_id,
            extra=req.get("extra"),
        )
        return {"event_id": event_id, "trace_id": trace_id, "saved": True}
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise HTTPException(status_code=400, detail="Internal server error")


def _dispatch_single(path: str, payload: dict) -> dict:
    """将单条批量事件分发到对应的 ingest 处理器。

    path 为 SDK 原始上报路径（如 /ingest/error），payload 为该路径对应的完整请求体。
    各路径的参数提取逻辑与独立 ingest 端点保持一致。
    """
    if path == "/ingest/error":
        return tool_ingest_error(
            exc_type=payload.get("exc_type", "UnknownError"),
            message=payload.get("message", ""),
            frames=payload.get("frames", []),
            source=payload.get("source", "http_ingest"),
            extra=payload.get("extra"),
            trace_id=payload.get("trace_id"),
            session_id=payload.get("session_id"),
        )
    if path == "/ingest/network":
        return tool_ingest_network(
            record=payload.get("record", {}),
            trace_id=payload.get("trace_id"),
            request_id=payload.get("request_id"),
        )
    if path == "/ingest/ui-event":
        trace_id = payload.get("trace_id")
        event_id = save_ui_event(
            event=payload.get("event", {}) or {},
            trace_id=trace_id,
            extra=payload.get("extra"),
        )
        return {"event_id": event_id, "trace_id": trace_id, "saved": True}
    if path == "/ingest/console":
        return tool_ingest_console(
            level=payload.get("level", "info"),
            message=payload.get("message", ""),
            source=payload.get("source", "browser_sdk"),
            extra=payload.get("extra"),
            trace_id=payload.get("trace_id"),
            request_id=payload.get("request_id"),
        )
    if path == "/ingest/silent-failure":
        return tool_ingest_silent_failure(
            message=payload.get("message", ""),
            frames=payload.get("frames"),
            ui_events=payload.get("ui_events"),
            network_records=payload.get("network_records"),
            expectation=payload.get("expectation"),
            observed=payload.get("observed"),
            observed_events=payload.get("observed_events"),
            source=payload.get("source", "browser_sdk"),
            extra=payload.get("extra"),
            trace_id=payload.get("trace_id"),
            session_id=payload.get("session_id"),
        )
    raise ValueError(f"Unknown ingest path: {path}")


@router.post("/batch")
def ingest_batch(req: dict):
    """批量上报端点 —— 接收事件数组并分发给各 ingest 处理器。

    请求体格式::

        {
          "events": [
            {"path": "/ingest/error", "payload": {...}},
            {"path": "/ingest/network", "payload": {...}},
            ...
          ]
        }

    每条事件独立处理，单条失败不影响其余事件。
    """
    events = req.get("events", [])
    if not isinstance(events, list):
        events = [events] if events else []

    results = []
    for event in events:
        path = event.get("path", "")
        payload = event.get("payload", {})
        try:
            result = _dispatch_single(path, payload)
            results.append({"path": path, "ok": True, "result": result})
        except ValueError as e:
            logger.error(str(e), exc_info=True)
            results.append({"path": path, "ok": False, "error": "Invalid request payload"})
        except Exception as e:
            logger.error(str(e), exc_info=True)
            results.append({"path": path, "ok": False, "error": "Internal server error"})

    return {"results": results, "count": len(results)}

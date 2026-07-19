"""MCP 传输路由 —— 符合 MCP Streamable HTTP 规范

支持三种方法：
  POST   /mcp   客户端 → 服务端消息（initialize / tools/list / tools/call / 通知）
  GET    /mcp   SSE 流（Accept: text/event-stream）或服务健康（其余情况）
  DELETE /mcp   终止会话

握手后服务端通过 `Mcp-Session-Id` 响应头维护会话。
"""
import json
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.config import settings
from app.mcp.protocol.jsonrpc import make_error, INVALID_REQUEST
from app.mcp.protocol.server import dispatch_raw, PROTOCOL_VERSION, CAPABILITIES
from app.mcp.transports.session import registry
from app.mcp.transports.sse import hub

logger = logging.getLogger("ai-debug-mcp.api.mcp")
router = APIRouter(prefix="/mcp", tags=["mcp"])


def _health_payload() -> dict:
    return {
        "protocol": "mcp",
        "version": PROTOCOL_VERSION,
        "service": settings.service_name,
        "capabilities": list(CAPABILITIES.keys()),
        "transports": ["streamable-http", "stdio"],
    }


@router.post("")
async def mcp_post(request: Request):
    raw = await request.body()
    if not raw:
        return Response(status_code=202)

    # 预解析以判断 method / id / session
    try:
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception as e:
        return JSONResponse(make_error(None, INVALID_REQUEST, f"无效 JSON: {e}"), status_code=400)

    method = parsed.get("method", "")
    req_id = parsed.get("id")
    session_id = request.headers.get("Mcp-Session-Id")

    # ── 会话建立/校验 ──
    if method == "initialize":
        sess = registry.create() if not session_id or not registry.get(session_id) else registry.get(session_id)
        session_id = sess.session_id
    else:
        if not session_id:
            return JSONResponse(
                make_error(req_id, INVALID_REQUEST, "缺少 Mcp-Session-Id"),
                status_code=400,
            )
        sess = registry.get(session_id)
        if not sess:
            return JSONResponse(
                make_error(req_id, INVALID_REQUEST, "MCP session not found, please re-initialize"),
                status_code=404,
            )
        if method == "notifications/initialized":
            registry.mark_initialized(session_id)
            return Response(status_code=202)
        # 其它请求（tools/list、tools/call 等）需要已初始化
        if not sess.initialized and method not in ("ping",):
            return JSONResponse(
                make_error(req_id, INVALID_REQUEST, "会话尚未完成初始化"),
                status_code=400,
            )

    # ── 分发 ──
    try:
        result = await dispatch_raw(raw)
    except Exception:
        logger.exception("MCP dispatch 异常")
        return JSONResponse(make_error(req_id, INVALID_REQUEST, "请求处理失败，详情见服务端日志"), status_code=400)

    # 通知类消息无 id，不返回响应体
    if req_id is None:
        resp = Response(status_code=202)
        resp.headers["Mcp-Session-Id"] = session_id
        return resp

    # 根据 Accept 决定返回 JSON 还是 SSE 流
    accept = request.headers.get("Accept", "")
    if "text/event-stream" in accept:
        async def event_gen():
            yield hub.format_event(result)
        sr = StreamingResponse(event_gen(), media_type="text/event-stream")
        sr.headers["Mcp-Session-Id"] = session_id
        return sr

    resp = JSONResponse(result)
    resp.headers["Mcp-Session-Id"] = session_id
    return resp


@router.get("")
async def mcp_get(request: Request):
    accept = request.headers.get("Accept", "")
    if "text/event-stream" not in accept:
        # 非 SSE → 返回服务健康信息（保持旧行为）
        return _health_payload()

    # SSE 流：服务端 → 客户端 推送通道
    session_id = request.headers.get("Mcp-Session-Id")
    if not session_id:
        return JSONResponse({"detail": "缺少 Mcp-Session-Id"}, status_code=400)
    if not registry.get(session_id):
        return JSONResponse({"detail": "MCP session not found, please re-initialize"}, status_code=404)

    q = hub.subscribe(session_id)

    async def event_stream():
        try:
            yield ": connected\n\n"
            while True:
                msg = await q.get()
                yield hub.format_event(msg)
        except asyncio.CancelledError:
            pass
        finally:
            hub.unsubscribe(session_id, q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete("")
async def mcp_delete(request: Request):
    session_id = request.headers.get("Mcp-Session-Id")
    if session_id:
        if not registry.get(session_id):
            return JSONResponse({"detail": "MCP session not found, please re-initialize"}, status_code=404)
        registry.delete(session_id)
    return Response(status_code=204)

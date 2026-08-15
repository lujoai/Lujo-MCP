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
from app.mcp.protocol.jsonrpc import make_error, PARSE_ERROR, INVALID_REQUEST, INVALID_PARAMS, INTERNAL_ERROR
from app.mcp.protocol.server import dispatch_raw, PROTOCOL_VERSION, CAPABILITIES
from app.mcp.transports.session import registry, SessionLimitExceeded
from app.mcp.transports.sse import hub
from app.mcp.tools import TOOL_ROLE_REQUIREMENTS

logger = logging.getLogger("lujo-mcp.api.mcp")
router = APIRouter(prefix="/mcp", tags=["mcp"])


def _health_payload() -> dict:
    return {
        "protocol": "mcp",
        "version": PROTOCOL_VERSION,
        "service": settings.service_name,
        "capabilities": list(CAPABILITIES.keys()),
        "transports": ["streamable-http", "stdio"],
    }


def _accepted_sse(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("Accept", "")


@router.post("")
async def mcp_post(request: Request):
    raw = await request.body()
    if not raw:
        return Response(status_code=202)

    # 预解析以判断 method / id / session
    try:
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return JSONResponse(make_error(None, PARSE_ERROR, "无效 JSON，详情见服务端日志"), status_code=400)

    method = parsed.get("method", "")
    req_id = parsed.get("id")
    session_id = request.headers.get("Mcp-Session-Id")

    # ── 会话建立/校验 ──
    if method == "initialize":
        try:
            sess = registry.create() if not session_id or not registry.get(session_id) else registry.get(session_id)
        except SessionLimitExceeded:
            return JSONResponse(
                make_error(req_id, INTERNAL_ERROR, "会话数已达上限，请稍后重试"),
                status_code=503,
            )
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
            hub.publish_notification(
                session_id,
                "notifications/session/ready",
                {"sessionId": session_id, "initialized": True},
            )
            resp = Response(status_code=202)
            resp.headers["Mcp-Session-Id"] = session_id
            return resp
        # 其它请求（tools/list、tools/call 等）需要已初始化
        if not sess.initialized and method not in ("ping",):
            return JSONResponse(
                make_error(req_id, INVALID_REQUEST, "会话尚未完成初始化"),
                status_code=400,
            )

    # ── RBAC：tools/call 工具级角色门控 ──
    if method == "tools/call":
        # FIX: P1-9i params 非 dict（list/str/null）时返回 -32602，避免 AttributeError → 500
        mcp_params = parsed.get("params")
        if not isinstance(mcp_params, dict):
            return JSONResponse(
                make_error(req_id, INVALID_PARAMS, "Invalid params"),
                status_code=400,
            )
        tool_name = mcp_params.get("name", "")
        required_roles = TOOL_ROLE_REQUIREMENTS.get(tool_name)
        if required_roles is None:
            # 未在 TOOL_ROLE_REQUIREMENTS 注册的工具默认需要 admin 角色（fail-closed）
            required_roles = ("admin",)
            logger.warning("工具 '%s' 未在 TOOL_ROLE_REQUIREMENTS 注册，默认要求 admin 角色", tool_name)
        # FIX: P1-7 语义修正 —— 与 app/auth/rbac.py require_role 保持一致：
        # 鉴权未启用（无 API Key，role 未注入）且 rbac_enabled=False 时向后
        # 兼容放行为 admin；rbac_enabled=True 但无 role 时 fail-closed 为 viewer。
        role = getattr(request.state, "role", None)
        if role is None:
            role = "admin" if not settings.rbac_enabled else "viewer"
        if role not in required_roles:
            return JSONResponse(
                make_error(req_id, INVALID_REQUEST,
                           f"权限不足：工具 '{tool_name}' 需要 {required_roles} 角色，当前为 '{role}'"),
                status_code=403,
            )

    # ── 分发 ──
    try:
        result = await dispatch_raw(raw)
    except Exception:
        logger.exception("MCP dispatch 异常")
        return JSONResponse(make_error(req_id, INTERNAL_ERROR, "内部错误，详情见服务端日志"), status_code=500)

    # 通知类消息无 id，不返回响应体
    if req_id is None:
        resp = Response(status_code=202)
        resp.headers["Mcp-Session-Id"] = session_id
        return resp

    # 根据 Accept 决定返回 JSON 还是 SSE 流
    if _accepted_sse(request) and session_id and hub.publish(session_id, result):
        resp = Response(status_code=202)
        resp.headers["Mcp-Session-Id"] = session_id
        return resp

    if _accepted_sse(request):
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
    if not _accepted_sse(request):
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
                if hub.is_close_event(msg):
                    break
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
        hub.close_session(session_id)
    return Response(status_code=204)

"""SSE 流式响应公共助手（FIX: R7-A3）。

此前 Dashboard 流带 ``Cache-Control: no-cache`` / ``X-Accel-Buffering: no``，
而 MCP 传输（POST /mcp SSE 回退、GET /mcp 流）与 /api/debug/analyze/stream
没有——nginx 默认 ``proxy_buffering on`` 会把心跳与事件攒批延迟（心跳防
反代空闲断流的能力随之失效），修复不对称。三处统一经本助手构造响应。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse


def create_sse_response(content: AsyncIterator[Any], **kwargs: Any) -> StreamingResponse:
    """构造 text/event-stream StreamingResponse，统一补缓冲控制头。"""
    resp = StreamingResponse(content, media_type="text/event-stream", **kwargs)
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # nginx 透传：禁缓冲，保实时性
    return resp

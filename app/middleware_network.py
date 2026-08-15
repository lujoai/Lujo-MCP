"""
inbound HTTP 请求采集中间件 —— 把进入服务的请求记录为 network 记录。

独立于 middleware.py 安全栈（不修改它）。通过 app.add_middleware 挂载，且在
setup_middleware 之前添加，使其位于安全栈最内层（仅记录已通过鉴权/限流/体积限制的请求）。

- 默认关闭（settings.network_capture_enabled），避免噪声与性能影响。
- 跳过 /、/health、/metrics 公共路径。
- 通过响应头 X-Debug-Request-Id 返回 request_id，便于调用方按 id 查询该 inbound 记录。
"""
import asyncio
import time
import uuid
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.runtime.core.trace_repo import save_network_record

logger = logging.getLogger("lujo-mcp.middleware.network")

_PUBLIC_PATHS = {"/", "/health", "/metrics"}


class NetworkCaptureMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.network_capture_enabled or request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        request_id = request.headers.get("X-Debug-Request-Id") or f"inbound-{uuid.uuid4().hex[:12]}"
        start = time.time()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Debug-Request-Id"] = request_id
            return response
        finally:
            try:
                # Phase 2 过渡桥：async 上下文中的同步 PG 写入用 to_thread
                # 包装，避免阻塞事件循环。Phase 3 全异步化后移除。
                await asyncio.to_thread(
                    save_network_record,
                    {
                        "direction": "inbound",
                        "method": request.method,
                        "url": str(request.url),
                        "status_code": status_code,
                        "duration_ms": round((time.time() - start) * 1000, 2),
                    },
                    request_id=request_id,
                )
            except Exception:
                logger.exception("inbound 网络记录失败")

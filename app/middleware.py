"""中间件层 —— 鉴权、CORS、速率限制、请求体限流、安全头、请求追踪"""

import time
import logging
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings
from app.state.store import get_state_store

logger = logging.getLogger("lujo-mcp.middleware")


# ── API Key 鉴权中间件 ──
class AuthMiddleware(BaseHTTPMiddleware):
    """简单的 Bearer Token / X-API-Key 鉴权（fail-closed）"""

    PUBLIC_PATHS = ("/", "/health", "/demo", "/demo/silent-failure", "/ai-debug.js")

    def __init__(self, app):
        super().__init__(app)
        # 多 key 轮换 + 单 key 向后兼容由 app.auth.key_rotation 统一管理
        from app.auth.key_rotation import auth_enabled
        self.enabled = auth_enabled()

    @staticmethod
    def _extract_key(request: Request) -> str:
        auth_header = request.headers.get("Authorization", "")
        api_key_header = request.headers.get("X-API-Key", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        if api_key_header:
            return api_key_header
        return ""

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)

        # CORS 预检（OPTIONS）免鉴权，直接放行交由 CORSMiddleware 处理
        if request.method == "OPTIONS":
            return await call_next(request)

        # 健康检查免鉴权
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        # 多 key 轮换：恒定时间比较在 app.auth.key_rotation 内部完成（遍历所有 key 不短路）
        key = self._extract_key(request)
        from app.auth.key_rotation import verify_api_key
        from app.auth.rbac import get_role_for_key
        if verify_api_key(key):
            # 注入角色到 request.state，供下游 FastAPI 依赖（require_role）使用
            request.state.role = get_role_for_key(key)
            return await call_next(request)

        # sendBeacon / EventSource 无法设置自定义 header（S1）：尝试短时 beacon 令牌。
        # 令牌仅对 scope 前缀（默认 /ingest）有效且短 TTL，避免永久 Key 出现在 URL。
        from app.auth.beacon import verify_beacon_token
        token = request.query_params.get("token", "")
        role = verify_beacon_token(token, request.url.path) if token else None
        if role is not None:
            request.state.role = role
            return await call_next(request)

        return JSONResponse(status_code=401, content={"detail": "Invalid API key"})


# ── 请求体大小限制中间件 ──
class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """防御超大请求体导致的 OOM / DoS

    - 带 Content-Length：先做硬检查，不读 body。
    - 无 Content-Length 或 chunked：用 request.stream() 流式累计字节数，
      超限即中断返回 413；未超限则把已读 body 回填到 request._body，
      交由下游路由重新读取（利用 Starlette _CachedRequest 的 _body 缓存机制）。
    """

    async def dispatch(self, request: Request, call_next):
        limit = settings.max_body_size

        # 带 Content-Length 的请求：先做硬检查
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"请求体过大，限制 {limit} 字节"},
                    )
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "无效的 Content-Length"})

        # 无 Content-Length 或 chunked transfer-encoding：流式累计计数，超限即中断
        transfer_encoding = request.headers.get("transfer-encoding", "")
        if not content_length or "chunked" in transfer_encoding.lower():
            total = 0
            exceeded = False
            chunks: list[bytes] = []
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    exceeded = True
                    break
                chunks.append(chunk)

            if exceeded:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"请求体过大，限制 {limit} 字节"},
                )

            # 未超限：回填已读 body，确保下游路由能重新读取（避免空 body 422）
            request._body = b"".join(chunks)

        return await call_next(request)


# ── 安全响应头中间件 ──
# FIX: SEC-1 统一所有响应类型（HTML/JSON/JS 等）的 CSP 头 —— 此前仅在
# dashboard/demo 的 HTML 响应上单独设置，其余响应未覆盖。
# 页面使用内联 <script>，故 script-src 需放行 'unsafe-inline'；
# default-src 'self' 仍阻止外域资源/脚本加载（纵深防御，主防线为 esc() 转义）。
_CSP_HEADER = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """为所有响应补充基础安全头"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP_HEADER)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        return response


# ── 速率限制中间件 ──
class RateLimitMiddleware(BaseHTTPMiddleware):
    """P1-3: 端点级限流 —— 不同端点设置不同的速率限制"""

    ENDPOINT_LIMITS = {
        "/ingest/": (120, 60),
        # FIX: R3-5 result 轮询子路径须先于 analyze 匹配（dict 有序，具体前缀在前），
        # 否则 GET /api/debug/analyze/result/{job_id} 被 analyze 的 10/min 误伤，
        # 合法客户端轮询超 10 次/分即 429
        "/api/debug/analyze/result": (60, 60),
        "/api/debug/analyze": (10, 60),
        "/api/debug/verify/ui": (5, 60),
    }

    async def dispatch(self, request: Request, call_next):
        try:
            client_ip = self._get_client_ip(request)
            store = get_state_store()
            path = request.url.path

            limit, window = self._get_endpoint_limit(path)
            key = f"ratelimit:{client_ip}:{path}"

            if not store.allow(key, limit, window):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests, please slow down"},
                )
        except Exception:
            # SEC-07: 与 store 层 fail-closed 语义对齐——
            # 状态后端初始化失败（如 Redis 不可用）等异常不再降级放行，
            # 而是拒绝请求，避免 Redis 故障时限流形同虚设。
            logger.exception("RateLimitMiddleware 异常，fail-closed 拒绝请求")
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit service unavailable"},
            )

        return await call_next(request)

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """SEC: 取真实客户端 IP，修复反代场景下限流键共享/绕过。

        - 反代场景（nginx/CloudFlare）：``request.client.host`` 是代理 IP，所有真实用户
          共享同一限流桶（互相误伤）；攻击者也可用代理池变化 IP 绕过。
        - 优先读 X-Forwarded-For 最左 IP（最原始客户端），再读 X-Real-IP。
        - 都缺失时回退 ``request.client.host``，兜底 "unknown"。

        X-Forwarded-For 形如 ``client, proxy1, proxy2``，取最左 client。空串跳过。
        """
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # 取最左非空 IP（最原始客户端），防代理链注入伪造后续段
            first = next((ip.strip() for ip in xff.split(",") if ip.strip()), "")
            if first:
                return first
        xri = request.headers.get("x-real-ip", "")
        if xri:
            ip = xri.strip()
            if ip:
                return ip
        return request.client.host if request.client else "unknown"

    @staticmethod
    def _get_endpoint_limit(path: str) -> tuple[int, int]:
        """根据路径获取限流配置，未匹配则使用全局默认值"""
        for prefix, (limit, window) in RateLimitMiddleware.ENDPOINT_LIMITS.items():
            if path.startswith(prefix):
                return limit, window
        return settings.rate_limit_per_minute, 60


# ── 请求追踪中间件 ──
class TraceMiddleware(BaseHTTPMiddleware):
    """给每个请求注入 trace_id，放入 response header；异常时也能记录日志"""

    async def dispatch(self, request: Request, call_next):
        import uuid
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id

        start = time.time()
        try:
            response: Response = await call_next(request)
        except Exception:
            elapsed = time.time() - start
            logger.exception(
                f"{request.method} {request.url.path} 异常",
                extra={"trace_id": trace_id, "elapsed_ms": round(elapsed * 1000, 2)},
            )
            raise

        elapsed = time.time() - start
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Response-Time"] = f"{elapsed:.3f}s"

        logger.info(
            "request", extra={
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsed_ms": round(elapsed * 1000, 2),
            }
        )
        return response


def setup_middleware(app: FastAPI):
    """在 FastAPI app 上批量注册中间件（顺序：外→内）。

    Starlette 中间件栈为 LIFO：后 add 的中间件位于栈顶（最外层），
    最先处理请求、最后处理响应。因此 CORSMiddleware 必须**最后** add，
    使其位于最外层，OPTIONS 预检先于 AuthMiddleware 鉴权处理，
    避免预检被 401 拦截。
    """
    # 内层中间件先注册（add 顺序：内→外）
    app.add_middleware(AuthMiddleware)
    app.add_middleware(MaxBodySizeMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceMiddleware)

    # CORS 默认收紧：cors_origins 为空串时不注册 CORSMiddleware（不下发 CORS 头）。
    # 显式设置 CORS_ORIGINS=* 时开放所有来源（opt-in）；否则按逗号分隔白名单。
    # 必须最后 add，使其成为最外层。
    if settings.cors_origins:
        if settings.cors_origins == "*":
            allow_origins = ["*"]
            allow_credentials = False  # 规范不允许 * 与 credentials 同时使用
        else:
            allow_origins = [
                o.strip() for o in settings.cors_origins.split(",") if o.strip()
            ]
            allow_credentials = True
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_credentials=allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

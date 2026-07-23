"""可观测性模块 —— Prometheus 指标 + 请求计数器"""

import re
import time
import threading
import logging
import hmac
from collections import defaultdict
from typing import Dict, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger("ai-debug-mcp.metrics")

# ── 线程安全指标存储 ──
_counter_lock = threading.Lock()
_request_total: Dict[Tuple[str, str, int], int] = defaultdict(int)
_error_total: Dict[Tuple[str, str], int] = defaultdict(int)
_latency_sum: Dict[str, float] = defaultdict(float)
_latency_count: Dict[str, int] = defaultdict(int)

router = APIRouter(tags=["observability"])


def _sanitize_label(value: str) -> str:
    """移除 label 值中的特殊字符（换行、制表符等），防止指标格式破坏"""
    return re.sub(r'[\n\r\t\x00-\x1f]', '', value)


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录每个请求的指标（计数 + 延迟）"""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        method = request.method
        # 使用路由模板作为 path label，避免基数爆炸
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path) if route else request.url.path
        path = _sanitize_label(path)

        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            elapsed = time.time() - start
            with _counter_lock:
                _request_total[(method, path, status)] += 1
                if status >= 500:
                    _error_total[(method, path)] += 1
                _latency_sum[path] += elapsed
                _latency_count[path] += 1

        return response


def _render_prometheus() -> str:
    """生成 Prometheus 文本格式指标"""
    lines = []

    lines.append("# HELP http_requests_total Total HTTP requests by method/path/status")
    lines.append("# TYPE http_requests_total counter")
    with _counter_lock:
        for (method, path, status), count in _request_total.items():
            lines.append(
                f'http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}'
            )

        lines.append("# HELP http_errors_total Total HTTP 5xx errors by method/path")
        lines.append("# TYPE http_errors_total counter")
        for (method, path), count in _error_total.items():
            lines.append(
                f'http_errors_total{{method="{method}",path="{path}"}} {count}'
            )

        lines.append("# HELP http_request_duration_seconds_sum Request latency sum (seconds)")
        lines.append("# TYPE http_request_duration_seconds_sum counter")
        for path, total in _latency_sum.items():
            lines.append(
                f'http_request_duration_seconds_sum{{path="{path}"}} {round(total, 4)}'
            )

        lines.append("# HELP http_request_duration_seconds_count Request latency count")
        lines.append("# TYPE http_request_duration_seconds_count counter")
        for path, count in _latency_count.items():
            lines.append(
                f'http_request_duration_seconds_count{{path="{path}"}} {count}'
            )

    return "\n".join(lines) + "\n"


@router.get("/metrics")
def metrics(request: Request):
    """Prometheus 指标端点

    SEC-08 独立鉴权 toggle：
    - METRICS_AUTH_ENABLED=False（默认）：不额外鉴权，依赖全局 AuthMiddleware
    - METRICS_AUTH_ENABLED=True：端点层独立校验 API Key（Bearer/X-API-Key），
      与全局中间件解耦，防止 AuthMiddleware 配置疏漏导致指标泄露
    """
    if settings.metrics_auth_enabled:
        auth_header = request.headers.get("Authorization", "")
        api_key_header = request.headers.get("X-API-Key", "")
        if auth_header.startswith("Bearer "):
            provided_key = auth_header[7:]
        elif api_key_header:
            provided_key = api_key_header
        else:
            provided_key = ""
        # 恆定时间比较，避免时序攻击
        if not hmac.compare_digest(provided_key, settings.api_key or ""):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key"},
            )
    return PlainTextResponse(_render_prometheus())


def setup_observability(app):
    """注册可观测性路由和中间件"""
    app.include_router(router)
    app.add_middleware(MetricsMiddleware)
    logger.info("可观测性模块已启用 (/metrics)")

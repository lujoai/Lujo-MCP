"""可观测性模块 —— Prometheus 指标 + OpenTelemetry（P3-4）

P3-4: 双模式设计——保留原有 /metrics Prometheus 文本端点（向后兼容），
同时引入 OpenTelemetry SDK 作为指标记录的主路径，支持 OTLP 导出。

- OTel 关闭时：仅使用原有内存存储 + Prometheus 文本端点
- OTel 开启时：同时向 OTel instruments 和内存存储写入，/metrics 仍可用
"""

import re
import time
import threading
import logging
from collections import defaultdict
from typing import Dict, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger("ai-debug-mcp.metrics")

# ── 线程安全指标存储（向后兼容）──
_counter_lock = threading.Lock()
_request_total: Dict[Tuple[str, str, int], int] = defaultdict(int)
_error_total: Dict[Tuple[str, str], int] = defaultdict(int)
_latency_sum: Dict[str, float] = defaultdict(float)
_latency_count: Dict[str, int] = defaultdict(int)

router = APIRouter(tags=["observability"])

# ── OpenTelemetry（P3-4）──
_otel_meter = None
_otel_request_counter = None
_otel_error_counter = None
_otel_latency_histogram = None
_otel_shutdown = None

try:
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.resources import Resource
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    logger.warning("OpenTelemetry 未安装，OTel 指标导出功能已禁用")


def _init_otel():
    """初始化 OpenTelemetry（惰性调用）。
    
    仅在 settings.otel_enabled=True 且 OTel 可用时生效。
    返回 (meter, request_counter, error_counter, latency_histogram, shutdown_func)
    或 (None, None, None, None, None) 如果禁用或不可用。
    """
    global _otel_meter, _otel_request_counter, _otel_error_counter, _otel_latency_histogram, _otel_shutdown

    if _otel_meter is not None:
        return _otel_meter, _otel_request_counter, _otel_error_counter, _otel_latency_histogram, _otel_shutdown

    if not _OTEL_AVAILABLE or not settings.otel_enabled:
        return None, None, None, None, None

    try:
        resource = Resource(attributes={
            "service.name": settings.otel_service_name,
        })

        exporter = None
        endpoint = settings.otel_exporter_endpoint
        if endpoint:
            exporter = OTLPMetricExporter(endpoint=endpoint)
        else:
            exporter = OTLPMetricExporter()

        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=settings.otel_metrics_interval_ms,
        )

        provider = MeterProvider(resource=resource, metric_readers=[reader])
        otel_metrics.set_meter_provider(provider)

        meter = provider.get_meter("ai-debug-mcp")

        request_counter = meter.create_counter(
            "http_requests_total",
            description="Total HTTP requests by method/path/status",
        )

        error_counter = meter.create_counter(
            "http_errors_total",
            description="Total HTTP 5xx errors by method/path",
        )

        latency_histogram = meter.create_histogram(
            "http_request_duration_seconds",
            description="HTTP request latency in seconds",
        )

        def shutdown():
            provider.shutdown()

        _otel_meter = meter
        _otel_request_counter = request_counter
        _otel_error_counter = error_counter
        _otel_latency_histogram = latency_histogram
        _otel_shutdown = shutdown

        logger.info("OpenTelemetry 指标导出已启用 (service=%s)", settings.otel_service_name)
        return meter, request_counter, error_counter, latency_histogram, shutdown

    except Exception as e:
        logger.error("OpenTelemetry 初始化失败，降级为仅 Prometheus 文本端点: %s", e)
        return None, None, None, None, None


def _sanitize_label(value: str) -> str:
    """移除 label 值中的特殊字符（换行、制表符等），防止指标格式破坏"""
    return re.sub(r'[\n\r\t\x00-\x1f]', '', value)


# FIX: P1-10b 指标表上限，防止高基数 key 撑爆内存
_MAX_METRIC_KEYS = 5000


def _trim_metric_tables_if_needed() -> None:
    """指标表超上限时清空重置（须持 _counter_lock）。"""
    if (
        len(_request_total) > _MAX_METRIC_KEYS
        or len(_error_total) > _MAX_METRIC_KEYS
        or len(_latency_sum) > _MAX_METRIC_KEYS
    ):
        logger.warning(
            "指标表超上限，清空重置 (request_total=%d, error_total=%d, latency=%d)",
            len(_request_total),
            len(_error_total),
            len(_latency_sum),
        )
        _request_total.clear()
        _error_total.clear()
        _latency_sum.clear()
        _latency_count.clear()


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录每个请求的指标（计数 + 延迟）"""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        method = request.method
        route = request.scope.get("route")
        if route is not None and getattr(route, "path", None):
            path = _sanitize_label(route.path)
        else:
            # FIX: P1-10b 未命中已注册路由时统一 "404-other"，
            # 否则完整 URL（含用户可控动态路径）会撑爆指标 key 表
            path = "404-other"

        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            elapsed = time.time() - start

            # ── 写入内存存储（向后兼容 /metrics 端点）──
            with _counter_lock:
                # FIX: P1-10b 写入前裁剪，防止高基数 key 无限增长
                _trim_metric_tables_if_needed()
                _request_total[(method, path, status)] += 1
                if status >= 500:
                    _error_total[(method, path)] += 1
                _latency_sum[path] += elapsed
                _latency_count[path] += 1

            # ── 写入 OpenTelemetry（P3-4）──
            _, req_counter, err_counter, latency_hist, _ = _init_otel()
            if req_counter:
                req_counter.add(1, {"method": method, "path": path, "status": str(status)})
            if err_counter and status >= 500:
                err_counter.add(1, {"method": method, "path": path})
            if latency_hist:
                latency_hist.record(elapsed, {"method": method, "path": path})

        return response


def _render_prometheus() -> str:
    """生成 Prometheus 文本格式指标（向后兼容）"""
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
      与全局中间件解耦，防止 AuthMiddleware 配置疏漏导致指标泄露。
      支持 API_KEYS 多 key 轮换 + API_KEY 向后兼容（复用 key_rotation.verify_api_key）。

    P3-4: 当 OTel 启用时，指标同时通过 OTLP 导出，此端点仍可用（向后兼容）。
    """
    if settings.metrics_auth_enabled:
        from app.auth.key_rotation import verify_api_key

        auth_header = request.headers.get("Authorization", "")
        api_key_header = request.headers.get("X-API-Key", "")
        if auth_header.startswith("Bearer "):
            provided_key = auth_header[7:]
        elif api_key_header:
            provided_key = api_key_header
        else:
            provided_key = ""
        if not verify_api_key(provided_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key"},
            )
    return PlainTextResponse(_render_prometheus())


def setup_observability(app):
    """注册可观测性路由和中间件"""
    app.include_router(router)
    app.add_middleware(MetricsMiddleware)

    if settings.otel_enabled:
        _init_otel()

    logger.info("可观测性模块已启用 (/metrics)")


def shutdown_observability():
    """优雅关闭可观测性模块（在 lifespan shutdown 中调用）"""
    if _otel_shutdown:
        try:
            _otel_shutdown()
            logger.info("OpenTelemetry 指标导出已关闭")
        except Exception:
            logger.warning("关闭 OpenTelemetry 时出错", exc_info=True)

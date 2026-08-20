"""可观测性模块 —— Prometheus 指标 + OpenTelemetry（P3-4 / v0.6.0 扩展）

P3-4: 双模式设计——保留原有 /metrics Prometheus 文本端点（向后兼容），
同时引入 OpenTelemetry SDK 作为指标记录的主路径，支持 OTLP 导出。

- OTel 关闭时：仅使用原有内存存储 + Prometheus 文本端点
- OTel 开启时：同时向 OTel instruments 和内存存储写入，/metrics 仍可用

v0.6.0 增强：
- LLM 请求统计、耗时、缓存命中与 Token 消耗监控
- 存储层（PG / Memory）操作延迟、成功率与重试监控
"""

import re
import time
import threading
import logging
from collections import defaultdict
from typing import Dict, Tuple, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger("lujo-mcp.metrics")

# ── 线程安全指标存储（向后兼容）──
_counter_lock = threading.Lock()

# HTTP 指标
_request_total: Dict[Tuple[str, str, int], int] = defaultdict(int)
_error_total: Dict[Tuple[str, str], int] = defaultdict(int)
_latency_sum: Dict[str, float] = defaultdict(float)
_latency_count: Dict[str, int] = defaultdict(int)

# v0.6.0: LLM 分析指标
_llm_requests_total: Dict[Tuple[str, str, str], int] = defaultdict(int)  # (provider, model, status) -> count
_llm_latency_sum: Dict[Tuple[str, str], float] = defaultdict(float)      # (provider, model) -> sum_sec
_llm_latency_count: Dict[Tuple[str, str], int] = defaultdict(int)        # (provider, model) -> count
_llm_cache_hits_total: Dict[str, int] = defaultdict(int)                 # cache_type -> count
_llm_tokens_total: Dict[str, int] = defaultdict(int)                     # "prompt" | "completion" -> count

# v0.6.0: 存储层指标
_storage_ops_total: Dict[Tuple[str, str, str], int] = defaultdict(int)   # (store, operation, status) -> count
_storage_latency_sum: Dict[Tuple[str, str], float] = defaultdict(float)  # (store, operation) -> sum_sec
_storage_latency_count: Dict[Tuple[str, str], int] = defaultdict(int)    # (store, operation) -> count
_pg_retries_total: Dict[str, int] = defaultdict(int)                     # operation -> count

router = APIRouter(tags=["observability"])

# ── OpenTelemetry（P3-4）──
_otel_meter = None
_otel_request_counter = None
_otel_error_counter = None
_otel_latency_histogram = None
_otel_llm_req_counter = None
_otel_llm_latency_hist = None
_otel_llm_token_counter = None
_otel_llm_cache_counter = None
_otel_storage_op_counter = None
_otel_storage_lat_hist = None
_otel_pg_retry_counter = None
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
    global _otel_meter, _otel_request_counter, _otel_error_counter, _otel_latency_histogram
    global _otel_llm_req_counter, _otel_llm_latency_hist, _otel_llm_token_counter, _otel_llm_cache_counter
    global _otel_storage_op_counter, _otel_storage_lat_hist, _otel_pg_retry_counter, _otel_shutdown

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

        meter = provider.get_meter("lujo-mcp")

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

        # v0.6.0 OTel 扩展 instruments
        llm_req_counter = meter.create_counter(
            "llm_requests_total",
            description="Total LLM analysis requests by provider/model/status",
        )
        llm_latency_hist = meter.create_histogram(
            "llm_request_duration_seconds",
            description="LLM analysis latency in seconds",
        )
        llm_token_counter = meter.create_counter(
            "llm_tokens_total",
            description="Total LLM tokens consumed",
        )
        llm_cache_counter = meter.create_counter(
            "llm_cache_hits_total",
            description="Total LLM cache hits by cache type",
        )
        storage_op_counter = meter.create_counter(
            "storage_operations_total",
            description="Total storage operations by store/op/status",
        )
        storage_lat_hist = meter.create_histogram(
            "storage_operation_duration_seconds",
            description="Storage operation latency in seconds",
        )
        pg_retry_counter = meter.create_counter(
            "pg_retries_total",
            description="Total PostgreSQL connection retries",
        )

        def shutdown():
            provider.shutdown()

        _otel_meter = meter
        _otel_request_counter = request_counter
        _otel_error_counter = error_counter
        _otel_latency_histogram = latency_histogram
        _otel_llm_req_counter = llm_req_counter
        _otel_llm_latency_hist = llm_latency_hist
        _otel_llm_token_counter = llm_token_counter
        _otel_llm_cache_counter = llm_cache_counter
        _otel_storage_op_counter = storage_op_counter
        _otel_storage_lat_hist = storage_lat_hist
        _otel_pg_retry_counter = pg_retry_counter
        _otel_shutdown = shutdown

        logger.info("OpenTelemetry 指标导出已启用 (service=%s)", settings.otel_service_name)
        return meter, request_counter, error_counter, latency_histogram, shutdown

    except Exception as e:
        logger.error("OpenTelemetry 初始化失败，降级为仅 Prometheus 文本端点: %s", e)
        return None, None, None, None, None


def _sanitize_label(value: str) -> str:
    """移除 label 值中的特殊字符（换行、制表符等），防止指标格式破坏"""
    return re.sub(r'[\n\r\t\x00-\x1f"]', '', str(value))


# FIX: P1-10b 指标表上限，防止高基数 key 撑爆内存
_MAX_METRIC_KEYS = 5000


def _trim_metric_tables_if_needed() -> None:
    """指标表超上限时清空重置（须持 _counter_lock）。"""
    total_keys = (
        len(_request_total)
        + len(_error_total)
        + len(_latency_sum)
        + len(_llm_requests_total)
        + len(_storage_ops_total)
    )
    if total_keys > _MAX_METRIC_KEYS:
        logger.warning("指标表超上限，清空重置 (total_keys=%d)", total_keys)
        _request_total.clear()
        _error_total.clear()
        _latency_sum.clear()
        _latency_count.clear()
        _llm_requests_total.clear()
        _llm_latency_sum.clear()
        _llm_latency_count.clear()
        _llm_cache_hits_total.clear()
        _llm_tokens_total.clear()
        _storage_ops_total.clear()
        _storage_latency_sum.clear()
        _storage_latency_count.clear()
        _pg_retries_total.clear()


# ── v0.6.0: LLM 记录辅助函数 ──

def record_llm_request(
    provider: str,
    model: str,
    status: str,
    duration_sec: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """记录 LLM 请求计数、延迟与 Token 消耗"""
    p = _sanitize_label(provider or "unknown")
    m = _sanitize_label(model or "unknown")
    s = _sanitize_label(status or "unknown")
    dur = max(0.0, float(duration_sec))

    with _counter_lock:
        _trim_metric_tables_if_needed()
        _llm_requests_total[(p, m, s)] += 1
        _llm_latency_sum[(p, m)] += dur
        _llm_latency_count[(p, m)] += 1
        if prompt_tokens > 0:
            _llm_tokens_total["prompt"] += int(prompt_tokens)
        if completion_tokens > 0:
            _llm_tokens_total["completion"] += int(completion_tokens)

    # OTel 双写
    _init_otel()
    if _otel_llm_req_counter:
        _otel_llm_req_counter.add(1, {"provider": p, "model": m, "status": s})
    if _otel_llm_latency_hist:
        _otel_llm_latency_hist.record(dur, {"provider": p, "model": m})
    if _otel_llm_token_counter:
        if prompt_tokens > 0:
            _otel_llm_token_counter.add(int(prompt_tokens), {"type": "prompt"})
        if completion_tokens > 0:
            _otel_llm_token_counter.add(int(completion_tokens), {"type": "completion"})


def record_llm_cache_hit(cache_type: str) -> None:
    """记录 LLM 缓存命中"""
    ct = _sanitize_label(cache_type or "unknown")
    with _counter_lock:
        _trim_metric_tables_if_needed()
        _llm_cache_hits_total[ct] += 1

    _init_otel()
    if _otel_llm_cache_counter:
        _otel_llm_cache_counter.add(1, {"cache_type": ct})


# ── v0.6.0: 存储层记录辅助函数 ──

def record_storage_operation(
    store: str,
    operation: str,
    status: str,
    duration_sec: float,
) -> None:
    """记录存储读写操作次数与耗时"""
    st = _sanitize_label(store or "unknown")
    op = _sanitize_label(operation or "unknown")
    stat = _sanitize_label(status or "unknown")
    dur = max(0.0, float(duration_sec))

    with _counter_lock:
        _trim_metric_tables_if_needed()
        _storage_ops_total[(st, op, stat)] += 1
        _storage_latency_sum[(st, op)] += dur
        _storage_latency_count[(st, op)] += 1

    _init_otel()
    if _otel_storage_op_counter:
        _otel_storage_op_counter.add(1, {"store": st, "operation": op, "status": stat})
    if _otel_storage_lat_hist:
        _otel_storage_lat_hist.record(dur, {"store": st, "operation": op})


def record_pg_retry(operation: str) -> None:
    """记录 PostgreSQL 断线重试操作"""
    op = _sanitize_label(operation or "unknown")
    with _counter_lock:
        _trim_metric_tables_if_needed()
        _pg_retries_total[op] += 1

    _init_otel()
    if _otel_pg_retry_counter:
        _otel_pg_retry_counter.add(1, {"operation": op})


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录每个请求的指标（计数 + 延迟）"""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        method = request.method

        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            elapsed = time.time() - start

            route = request.scope.get("route")
            if route is not None and getattr(route, "path", None):
                path = _sanitize_label(route.path)
            else:
                path = "404-other"

            # ── 写入内存存储（向后兼容 /metrics 端点）──
            with _counter_lock:
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

    # 1. HTTP 请求计数
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

        # 2. LLM 分析请求与 Token
        if _llm_requests_total:
            lines.append("# HELP llm_requests_total Total LLM analysis requests by provider/model/status")
            lines.append("# TYPE llm_requests_total counter")
            for (provider, model, status), count in _llm_requests_total.items():
                lines.append(
                    f'llm_requests_total{{provider="{provider}",model="{model}",status="{status}"}} {count}'
                )

        if _llm_latency_sum:
            lines.append("# HELP llm_request_duration_seconds_sum LLM latency sum (seconds)")
            lines.append("# TYPE llm_request_duration_seconds_sum counter")
            for (provider, model), total in _llm_latency_sum.items():
                lines.append(
                    f'llm_request_duration_seconds_sum{{provider="{provider}",model="{model}"}} {round(total, 4)}'
                )
            lines.append("# HELP llm_request_duration_seconds_count LLM latency count")
            lines.append("# TYPE llm_request_duration_seconds_count counter")
            for (provider, model), count in _llm_latency_count.items():
                lines.append(
                    f'llm_request_duration_seconds_count{{provider="{provider}",model="{model}"}} {count}'
                )

        if _llm_cache_hits_total:
            lines.append("# HELP llm_cache_hits_total Total LLM cache hits by cache type")
            lines.append("# TYPE llm_cache_hits_total counter")
            for cache_type, count in _llm_cache_hits_total.items():
                lines.append(
                    f'llm_cache_hits_total{{cache_type="{cache_type}"}} {count}'
                )

        if _llm_tokens_total:
            lines.append("# HELP llm_tokens_total Total LLM tokens consumed")
            lines.append("# TYPE llm_tokens_total counter")
            for tok_type, count in _llm_tokens_total.items():
                lines.append(
                    f'llm_tokens_total{{type="{tok_type}"}} {count}'
                )

        # 3. 存储层操作与重试
        if _storage_ops_total:
            lines.append("# HELP storage_operations_total Total storage operations by store/op/status")
            lines.append("# TYPE storage_operations_total counter")
            for (store, op, status), count in _storage_ops_total.items():
                lines.append(
                    f'storage_operations_total{{store="{store}",operation="{op}",status="{status}"}} {count}'
                )

        if _storage_latency_sum:
            lines.append("# HELP storage_operation_duration_seconds_sum Storage latency sum (seconds)")
            lines.append("# TYPE storage_operation_duration_seconds_sum counter")
            for (store, op), total in _storage_latency_sum.items():
                lines.append(
                    f'storage_operation_duration_seconds_sum{{store="{store}",operation="{op}"}} {round(total, 4)}'
                )
            lines.append("# HELP storage_operation_duration_seconds_count Storage latency count")
            lines.append("# TYPE storage_operation_duration_seconds_count counter")
            for (store, op), count in _storage_latency_count.items():
                lines.append(
                    f'storage_operation_duration_seconds_count{{store="{store}",operation="{op}"}} {count}'
                )

        if _pg_retries_total:
            lines.append("# HELP pg_retries_total Total PostgreSQL connection retries")
            lines.append("# TYPE pg_retries_total counter")
            for op, count in _pg_retries_total.items():
                lines.append(
                    f'pg_retries_total{{operation="{op}"}} {count}'
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

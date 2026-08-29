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
from typing import Dict, Tuple

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

# v0.6.2: MCP 工具执行与背压指标
_mcp_tool_calls_total: Dict[Tuple[str, str], int] = defaultdict(int)        # (tool_name, status) -> count
_mcp_tool_latency_sum: Dict[str, float] = defaultdict(float)                # tool_name -> sum_sec
_mcp_tool_latency_count: Dict[str, int] = defaultdict(int)                  # tool_name -> count
_mcp_tool_busy_rejected_total: Dict[Tuple[str, str], int] = defaultdict(int)  # (tool_name, pool_type) -> count
_mcp_tool_wait_latency_sum: Dict[Tuple[str, str], float] = defaultdict(float)   # (tool_name, pool_type) -> sum_sec
_mcp_tool_wait_latency_count: Dict[Tuple[str, str], int] = defaultdict(int) # (tool_name, pool_type) -> count

# v0.7.0: KB 学习闭环指标（R7-P1-2 闭环复活的观测面）
_kb_hits_total: Dict[str, int] = defaultdict(int)                   # level -> count
_kb_writeback_total: Dict[Tuple[str, str], int] = defaultdict(int)  # (kind, status) -> count
_kb_experience_recall_total: Dict[str, int] = defaultdict(int)      # status -> count
_KB_HIT_LEVELS = ("l1_fingerprint", "l1_5_normalized", "l2_type", "vector_rag", "miss")

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
_otel_mcp_tool_counter = None
_otel_mcp_tool_lat_hist = None
_otel_mcp_busy_counter = None
_otel_mcp_wait_lat_hist = None
_otel_kb_hit_counter = None
_otel_kb_writeback_counter = None
_otel_kb_experience_counter = None
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
    global _otel_mcp_tool_counter, _otel_mcp_tool_lat_hist, _otel_mcp_busy_counter, _otel_mcp_wait_lat_hist
    global _otel_kb_hit_counter, _otel_kb_writeback_counter, _otel_kb_experience_counter

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
        _otel_mcp_tool_counter = meter.create_counter(
            "mcp_tool_calls_total",
            description="Total MCP tool calls by tool name and status",
        )
        _otel_mcp_tool_lat_hist = meter.create_histogram(
            "mcp_tool_duration_seconds",
            description="MCP tool execution latency in seconds",
        )
        _otel_mcp_busy_counter = meter.create_counter(
            "mcp_tool_busy_rejected_total",
            description="Total MCP tool calls rejected due to saturated execution queue",
        )
        _otel_mcp_wait_lat_hist = meter.create_histogram(
            "mcp_tool_queue_wait_duration_seconds",
            description="MCP tool slot acquisition wait latency in seconds",
        )
        _otel_kb_hit_counter = meter.create_counter(
            "kb_hits_total",
            description="Total KB lookup hits by match level (v0.7.0 learning-loop observability)",
        )
        _otel_kb_writeback_counter = meter.create_counter(
            "kb_writeback_total",
            description="Total KB writeback attempts by kind and status (v0.7.0)",
        )
        _otel_kb_experience_counter = meter.create_counter(
            "kb_experience_recall_total",
            description="Total debug-experience recalls by status (v0.7.0)",
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
        + len(_mcp_tool_calls_total)
        + len(_mcp_tool_busy_rejected_total)
        + len(_kb_hits_total)
        + len(_kb_writeback_total)
        + len(_kb_experience_recall_total)
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
        _mcp_tool_calls_total.clear()
        _mcp_tool_latency_sum.clear()
        _mcp_tool_latency_count.clear()
        _mcp_tool_busy_rejected_total.clear()
        _mcp_tool_wait_latency_sum.clear()
        _mcp_tool_wait_latency_count.clear()
        _kb_hits_total.clear()
        _kb_writeback_total.clear()
        _kb_experience_recall_total.clear()


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


# ── v0.6.2: MCP 工具记录辅助函数 ──

def record_mcp_tool_call(
    tool_name: str,
    status: str,
    duration_sec: float = 0.0,
) -> None:
    """记录 MCP 工具调用次数与执行耗时 (status: ok | error | busy | timeout)"""
    t = _sanitize_label(tool_name or "unknown")
    s = _sanitize_label(status or "unknown")
    dur = max(0.0, float(duration_sec))

    with _counter_lock:
        _trim_metric_tables_if_needed()
        _mcp_tool_calls_total[(t, s)] += 1
        if dur > 0.0 or s == "ok":
            _mcp_tool_latency_sum[t] += dur
            _mcp_tool_latency_count[t] += 1

    _init_otel()
    if _otel_mcp_tool_counter:
        _otel_mcp_tool_counter.add(1, {"tool": t, "status": s})
    if _otel_mcp_tool_lat_hist and (dur > 0.0 or s == "ok"):
        _otel_mcp_tool_lat_hist.record(dur, {"tool": t, "status": s})


def record_mcp_tool_busy(
    tool_name: str,
    pool_type: str = "light",
    wait_sec: float = 0.0,
) -> None:
    """记录 MCP 工具因槽位已满触发的 TOOL_BUSY 拒绝及等待耗时"""
    t = _sanitize_label(tool_name or "unknown")
    pool = _sanitize_label(pool_type or "light")
    w = max(0.0, float(wait_sec))

    with _counter_lock:
        _trim_metric_tables_if_needed()
        _mcp_tool_busy_rejected_total[(t, pool)] += 1
        if w > 0.0:
            _mcp_tool_wait_latency_sum[(t, pool)] += w
            _mcp_tool_wait_latency_count[(t, pool)] += 1

    _init_otel()
    if _otel_mcp_busy_counter:
        _otel_mcp_busy_counter.add(1, {"tool": t, "pool": pool})
    if _otel_mcp_wait_lat_hist and w > 0.0:
        _otel_mcp_wait_lat_hist.record(w, {"tool": t, "pool": pool})


def record_mcp_tool_wait(
    tool_name: str,
    pool_type: str = "light",
    wait_sec: float = 0.0,
) -> None:
    """记录 MCP 工具成功获取槽位前的排队等待耗时"""
    t = _sanitize_label(tool_name or "unknown")
    pool = _sanitize_label(pool_type or "light")
    w = max(0.0, float(wait_sec))

    if w <= 0.0:
        return

    with _counter_lock:
        _trim_metric_tables_if_needed()
        _mcp_tool_wait_latency_sum[(t, pool)] += w
        _mcp_tool_wait_latency_count[(t, pool)] += 1

    _init_otel()
    if _otel_mcp_wait_lat_hist:
        _otel_mcp_wait_lat_hist.record(w, {"tool": t, "pool": pool})


# ── v0.7.0: KB 学习闭环记录辅助函数 ──
# 观测目标（R7-P1-2 闭环复活的"在学"证明）：三级命中 / 向量 RAG / 分析与
# verify 回写 / 经验召回。record_* 全函数体防御：任何异常吞掉记 debug，
# 埋点永不影响业务返回值/异常传播。


def record_kb_hit(level: str) -> None:
    """记录 KB 命中层级计数。

    level: l1_fingerprint（精确指纹）| l1_5_normalized（归一化指纹）|
           l2_type（类型级 Jaccard）| vector_rag | miss
    """
    try:
        lv = _sanitize_label(level or "unknown")
        with _counter_lock:
            _trim_metric_tables_if_needed()
            _kb_hits_total[lv] += 1

        _init_otel()
        if _otel_kb_hit_counter:
            _otel_kb_hit_counter.add(1, {"level": lv})
    except Exception:
        logger.debug("record_kb_hit 埋点失败（忽略）", exc_info=True)


def record_kb_writeback(kind: str, status: str) -> None:
    """记录 KB 回写计数。

    kind: analysis（分析回写）| verify（verify_loop 写回）
    status: success | failed | skipped（未启用/无指纹等前置短路）| miss（未命中条目）
    """
    try:
        k = _sanitize_label(kind or "unknown")
        st = _sanitize_label(status or "unknown")
        with _counter_lock:
            _trim_metric_tables_if_needed()
            _kb_writeback_total[(k, st)] += 1

        _init_otel()
        if _otel_kb_writeback_counter:
            _otel_kb_writeback_counter.add(1, {"kind": k, "status": st})
    except Exception:
        logger.debug("record_kb_writeback 埋点失败（忽略）", exc_info=True)


def record_kb_experience_recall(status: str) -> None:
    """记录调试经验召回计数（status: hit | miss）"""
    try:
        st = _sanitize_label(status or "unknown")
        with _counter_lock:
            _trim_metric_tables_if_needed()
            _kb_experience_recall_total[st] += 1

        _init_otel()
        if _otel_kb_experience_counter:
            _otel_kb_experience_counter.add(1, {"status": st})
    except Exception:
        logger.debug("record_kb_experience_recall 埋点失败（忽略）", exc_info=True)


def get_kb_metric_snapshot() -> dict:
    """KB 学习闭环指标只读快照（dashboard kb-stats 端点用，进程级累计值）。

    键集合恒定（未发生的层级/状态为零值），前端无需判空。
    """
    with _counter_lock:
        return {
            "hits_by_level": {lvl: int(_kb_hits_total.get(lvl, 0)) for lvl in _KB_HIT_LEVELS},
            "writeback": {
                "analysis": {
                    s: int(_kb_writeback_total.get(("analysis", s), 0))
                    for s in ("success", "failed", "skipped")
                },
                "verify": {
                    s: int(_kb_writeback_total.get(("verify", s), 0))
                    for s in ("success", "miss", "skipped")
                },
            },
            "experience_recall": {
                s: int(_kb_experience_recall_total.get(s, 0)) for s in ("hit", "miss")
            },
        }


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

        # 4. v0.6.2: MCP 工具执行与背压指标
        if _mcp_tool_calls_total:
            lines.append("# HELP mcp_tool_calls_total Total MCP tool calls by tool/status")
            lines.append("# TYPE mcp_tool_calls_total counter")
            for (tool, status), count in _mcp_tool_calls_total.items():
                lines.append(
                    f'mcp_tool_calls_total{{tool="{tool}",status="{status}"}} {count}'
                )

        if _mcp_tool_latency_sum:
            lines.append("# HELP mcp_tool_duration_seconds_sum MCP tool execution latency sum (seconds)")
            lines.append("# TYPE mcp_tool_duration_seconds_sum counter")
            for tool, total in _mcp_tool_latency_sum.items():
                lines.append(
                    f'mcp_tool_duration_seconds_sum{{tool="{tool}"}} {round(total, 4)}'
                )
            lines.append("# HELP mcp_tool_duration_seconds_count MCP tool execution count")
            lines.append("# TYPE mcp_tool_duration_seconds_count counter")
            for tool, count in _mcp_tool_latency_count.items():
                lines.append(
                    f'mcp_tool_duration_seconds_count{{tool="{tool}"}} {count}'
                )

        if _mcp_tool_busy_rejected_total:
            lines.append("# HELP mcp_tool_busy_rejected_total Total MCP tool calls rejected due to queue saturation")
            lines.append("# TYPE mcp_tool_busy_rejected_total counter")
            for (tool, pool), count in _mcp_tool_busy_rejected_total.items():
                lines.append(
                    f'mcp_tool_busy_rejected_total{{tool="{tool}",pool="{pool}"}} {count}'
                )

        if _mcp_tool_wait_latency_sum:
            lines.append("# HELP mcp_tool_queue_wait_duration_seconds_sum MCP tool slot wait latency sum (seconds)")
            lines.append("# TYPE mcp_tool_queue_wait_duration_seconds_sum counter")
            for (tool, pool), total in _mcp_tool_wait_latency_sum.items():
                lines.append(
                    f'mcp_tool_queue_wait_duration_seconds_sum{{tool="{tool}",pool="{pool}"}} {round(total, 4)}'
                )
            lines.append("# HELP mcp_tool_queue_wait_duration_seconds_count MCP tool slot wait count")
            lines.append("# TYPE mcp_tool_queue_wait_duration_seconds_count counter")
            for (tool, pool), count in _mcp_tool_wait_latency_count.items():
                lines.append(
                    f'mcp_tool_queue_wait_duration_seconds_count{{tool="{tool}",pool="{pool}"}} {count}'
                )

        # 5. v0.7.0: KB 学习闭环
        if _kb_hits_total:
            lines.append("# HELP kb_hits_total Total KB lookup hits by match level")
            lines.append("# TYPE kb_hits_total counter")
            for level, count in _kb_hits_total.items():
                lines.append(
                    f'kb_hits_total{{level="{level}"}} {count}'
                )

        if _kb_writeback_total:
            lines.append("# HELP kb_writeback_total Total KB writeback attempts by kind/status")
            lines.append("# TYPE kb_writeback_total counter")
            for (kind, status), count in _kb_writeback_total.items():
                lines.append(
                    f'kb_writeback_total{{kind="{kind}",status="{status}"}} {count}'
                )

        if _kb_experience_recall_total:
            lines.append("# HELP kb_experience_recall_total Total debug-experience recalls by status")
            lines.append("# TYPE kb_experience_recall_total counter")
            for status, count in _kb_experience_recall_total.items():
                lines.append(
                    f'kb_experience_recall_total{{status="{status}"}} {count}'
                )

    return "\n".join(lines) + "\n"


@router.get("/metrics")
def metrics(request: Request):
    """Prometheus 指标端点

    SEC-08 独立鉴权 toggle：
    - METRICS_AUTH_ENABLED=False（默认）：不额外鉴权。P2-F2 起全局 AuthMiddleware 同时
      豁免 /metrics，即端点无鉴权可访问（供 Prometheus 等监控栈无凭据抓取；生产应按安全
      策略只把该端点发布到可信内网）。
    - METRICS_AUTH_ENABLED=True：端点层独立校验 API Key（Bearer/X-API-Key），
      与全局中间件解耦（全局中间件对其保持保护），防止 AuthMiddleware 配置疏漏导致指标泄露。
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

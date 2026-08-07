"""
兼容 shim：stacktrace 已迁移至 app.runtime.collectors.stacktrace。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.stacktrace import (
    SENSITIVE_KEYS,
    capture_exception,
    format_trace_for_ai,
)

__all__ = ["SENSITIVE_KEYS", "capture_exception", "format_trace_for_ai"]

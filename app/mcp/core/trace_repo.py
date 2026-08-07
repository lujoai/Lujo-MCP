"""
兼容 shim：trace_repo 已迁移至 app.runtime.core.trace_repo。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.trace_repo import (
    get_console_logs,
    get_network_records,
    get_trace,
    get_ui_events,
    save_console_log,
    save_network_record,
    save_trace,
    save_ui_event,
)

__all__ = [
    "get_console_logs",
    "get_network_records",
    "get_trace",
    "get_ui_events",
    "save_console_log",
    "save_network_record",
    "save_trace",
    "save_ui_event",
]

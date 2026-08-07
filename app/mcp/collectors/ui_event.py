"""
兼容 shim：ui_event 已迁移至 app.runtime.collectors.ui_event。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.ui_event import parse_ui_event, parse_ui_events

__all__ = ["parse_ui_event", "parse_ui_events"]

"""
兼容 shim：storage factory 已迁移至 app.runtime.core.storage.factory。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.storage.factory import (
    get_error_store,
    get_session_store,
    get_spec_store,
    get_trace_store,
)

__all__ = [
    "get_trace_store",
    "get_session_store",
    "get_error_store",
    "get_spec_store",
]

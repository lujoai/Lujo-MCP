"""
兼容 shim：storage base 已迁移至 app.runtime.core.storage.base。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.storage.base import (
    ErrorStorage,
    SessionStorage,
    SpecStorage,
    TraceStorage,
)

__all__ = [
    "TraceStorage",
    "SessionStorage",
    "ErrorStorage",
    "SpecStorage",
]

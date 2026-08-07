"""
兼容 shim：noop_store 已迁移至 app.runtime.core.storage.noop_store。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.storage.noop_store import (
    NoOpErrorStore,
    NoOpSpecStore,
)

__all__ = [
    "NoOpErrorStore",
    "NoOpSpecStore",
]

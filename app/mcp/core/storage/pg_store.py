"""
兼容 shim：pg_store 已迁移至 app.runtime.core.storage.pg_store。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.storage.pg_store import (
    PGErrorStore,
    PGSessionStore,
    PGSpecStore,
    PGTraceStore,
    close_pool,
)

__all__ = [
    "PGTraceStore",
    "PGSessionStore",
    "PGErrorStore",
    "PGSpecStore",
    "close_pool",
]

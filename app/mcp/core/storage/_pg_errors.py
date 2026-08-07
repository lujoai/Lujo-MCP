"""
兼容 shim：_pg_errors 已迁移至 app.runtime.core.storage._pg_errors。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.storage._pg_errors import sanitize_pg_error

__all__ = [
    "sanitize_pg_error",
]

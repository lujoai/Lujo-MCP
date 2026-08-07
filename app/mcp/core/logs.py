"""
兼容 shim：logs 已迁移至 app.runtime.core.logs。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.logs import (
    add_log,
    add_logs_batch,
    create_request_id,
    delete_logs,
    get_logs,
    list_request_ids,
)

__all__ = [
    "add_log",
    "add_logs_batch",
    "create_request_id",
    "delete_logs",
    "get_logs",
    "list_request_ids",
]

"""
兼容 shim：errors 已迁移至 app.runtime.core.errors。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.errors import (
    aggregate_by_fingerprint,
    compute_fingerprint,
    get_by_id,
    get_latest,
    list_recent,
    query_pg_errors,
    rank_by_impact,
    record,
    search,
)

__all__ = [
    "aggregate_by_fingerprint",
    "compute_fingerprint",
    "get_by_id",
    "get_latest",
    "list_recent",
    "query_pg_errors",
    "rank_by_impact",
    "record",
    "search",
]

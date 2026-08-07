"""
兼容 shim：spec_store 已迁移至 app.runtime.verifier.spec_store。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.verifier.spec_store import (
    create,
    delete,
    get,
    list_specs,
    update,
    clear,
)

__all__ = ["create", "delete", "get", "list_specs", "update", "clear"]
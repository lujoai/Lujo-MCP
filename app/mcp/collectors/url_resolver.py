"""
兼容 shim：url_resolver 已迁移至 app.runtime.collectors.url_resolver。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.url_resolver import (
    _path_to_regex,
    resolve,
)

__all__ = ["_path_to_regex", "resolve"]

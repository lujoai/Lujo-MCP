"""
兼容 shim：spec 已迁移至 app.runtime.collectors.spec。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.spec import (
    SPEC_CANDIDATES,
    SPEC_SUFFIXES,
    discover_spec_files,
    get_project_specs,
    get_related_specs,
    match_specs,
    parse_spec_file,
    reload_specs,
)

__all__ = [
    "SPEC_CANDIDATES",
    "SPEC_SUFFIXES",
    "discover_spec_files",
    "get_project_specs",
    "get_related_specs",
    "match_specs",
    "parse_spec_file",
    "reload_specs",
]

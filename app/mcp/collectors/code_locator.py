"""
兼容 shim：code_locator 已迁移至 app.runtime.collectors.code_locator。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.code_locator import (
    get_code_snippet,
    get_snippets_for_frames,
    make_ide_link,
)

__all__ = ["get_code_snippet", "get_snippets_for_frames", "make_ide_link"]

"""
兼容 shim：context builder 已迁移至 app.runtime.context.builder。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.context.builder import build_context, build_debug_context

__all__ = ["build_context", "build_debug_context"]

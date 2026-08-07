"""
兼容 shim：git 已迁移至 app.runtime.core.git。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.git import get_blame_for_frame, get_recent_diff

__all__ = ["get_blame_for_frame", "get_recent_diff"]
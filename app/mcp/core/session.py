"""
兼容 shim：session 已迁移至 app.runtime.core.session。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.session import SessionManager, session_manager

__all__ = ["SessionManager", "session_manager"]

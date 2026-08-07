"""
兼容 shim：exception_hook 已迁移至 app.runtime.hooks.exception_hook。
仅重导出公开符号，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.hooks.exception_hook import install_global_hook, uninstall_global_hook

__all__ = ["install_global_hook", "uninstall_global_hook"]
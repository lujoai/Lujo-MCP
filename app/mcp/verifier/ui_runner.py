"""
兼容 shim：ui_runner 已迁移至 app.runtime.verifier.ui_runner。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.verifier.ui_runner import (
    inspect_url_security,
    is_available,
    is_safe_url,
    run_ui_verification,
)

__all__ = ["inspect_url_security", "is_available", "is_safe_url", "run_ui_verification"]
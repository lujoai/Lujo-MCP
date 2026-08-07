"""
兼容 shim：redaction 已迁移至 app.runtime.core.redaction。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.core.redaction import redact

__all__ = ["redact"]

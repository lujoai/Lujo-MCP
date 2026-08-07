"""
兼容 shim：assert_engine 已迁移至 app.runtime.verifier.assert_engine。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.verifier.assert_engine import assert_behavior

__all__ = ["assert_behavior"]
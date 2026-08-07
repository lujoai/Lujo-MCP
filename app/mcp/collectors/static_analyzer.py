"""
兼容 shim：static_analyzer 已迁移至 app.runtime.collectors.static_analyzer。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.static_analyzer import (
    FaultLocation,
    FunctionInfo,
    analyze,
    analyze_handler,
)

__all__ = ["FaultLocation", "FunctionInfo", "analyze", "analyze_handler"]

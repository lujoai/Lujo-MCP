"""
兼容 shim：runtime 已迁移至 app.runtime.collectors.runtime。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.runtime import (
    collect_process_info,
    collect_python_info,
    collect_runtime_snapshot,
    collect_system_info,
)

__all__ = [
    "collect_process_info",
    "collect_python_info",
    "collect_runtime_snapshot",
    "collect_system_info",
]

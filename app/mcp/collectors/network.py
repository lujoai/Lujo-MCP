"""
兼容 shim：network 已迁移至 app.runtime.collectors.network。
仅重导出，保持旧路径 import 链可用（Phase 0 迁移期间）。
"""
from app.runtime.collectors.network import parse_network_record, parse_network_records

__all__ = ["parse_network_record", "parse_network_records"]

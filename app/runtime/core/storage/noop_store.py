"""存储降级实现 —— memory 后端 ErrorStorage / SpecStorage 的 no-op 版本。

方案 C 拆分：ErrorStorage / SpecStorage 由 PG 后端实现真实持久化；
memory 后端不持久化 errors/specs（errors 走 errors.py 内存、specs 走
spec_store.py 内存 + trace_store 双写回退），因此工厂对非 PG 后端返回
本模块的 no-op 实现，保持调用方契约一致同时零行为变更。
"""

import logging
from typing import Optional

from app.runtime.core.storage.base import ErrorStorage, SpecStorage

logger = logging.getLogger("lujo-mcp.storage.noop")


class NoOpErrorStore(ErrorStorage):
    """memory 后端的错误存储 no-op 实现。"""

    def upsert_error(self, record_data: dict) -> None:
        # memory 后端错误持久化由 errors.py 内存队列承担，此处 no-op
        return None


class NoOpSpecStore(SpecStorage):
    """memory 后端的 spec 存储 no-op 实现。"""

    def save_spec(self, spec: dict) -> None:
        # memory 后端 spec 持久化由 spec_store.py 内存 + trace_store 双写承担
        return None

    def get_spec(self, spec_id: str) -> Optional[dict]:
        return None

    def list_specs(
        self,
        kind: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[dict]:
        return []

    def delete_spec(self, spec_id: str) -> bool:
        return False
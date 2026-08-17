"""存储降级实现 —— memory 后端 ErrorStorage / SpecStorage 的 no-op 版本。

方案 C 拆分：ErrorStorage / SpecStorage 由 PG 后端实现真实持久化；
memory 后端不持久化 errors/specs（errors 走 errors.py 内存、specs 走
spec_store.py 内存 + trace_store 双写回退），因此工厂对非 PG 后端返回
本模块的 no-op 实现，保持调用方契约一致同时零行为变更。
"""

import logging
from typing import Optional

from app.runtime.core.storage.base import ErrorStorage, SpecStorage, KnowledgeBaseStorage

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


class NoOpKnowledgeBaseStore(KnowledgeBaseStorage):
    """memory 后端的知识库持久化 no-op 实现（v0.5.3）。

    KB 主存 KnowledgeBaseStore 本身就在进程内，memory 后端下
    持久化层无事可做：写穿全部 no-op，启动回灌返回空列表，
    行为与历史版本完全一致。
    """

    def upsert_kb_entry(self, entry: dict) -> None:
        return None

    def update_kb_verification(
        self,
        fingerprint: str,
        verify_count: int,
        case_confidence: float,
        updated_at: float,
    ) -> bool:
        return False

    def delete_kb_entry(self, fingerprint: str) -> bool:
        return False

    def delete_all_kb_entries(self) -> int:
        return 0

    def list_recent_kb_entries(self, limit: int = 100) -> list[dict]:
        return []
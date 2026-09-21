"""存储降级实现 —— ErrorStorage / SpecStorage / KnowledgeBaseStorage 的 no-op 版本。

方案 C 拆分把这三类存储收敛为 ABC 契约；本模块提供「什么都不做」的实现，
让调用方无需在调用点判断后端。

W13 文案更正（PostgreSQL 运行时后端已随 Step 3 移除，原文案已失真）：
- ``NoOpErrorStore`` / ``NoOpSpecStore``：memory 后端下 errors 走 ``errors.py``
  的进程内队列、specs 走 ``spec_store.py`` 的进程内主存 + trace_store 双写备份，
  持久层无事可做，故 factory 返回 no-op；
- ``NoOpKnowledgeBaseStore``：仅在 ``KB_PERSIST_ENABLED=false``（显式关闭）或
  SQLite 笔记本初始化失败降级时使用。**开启时用的是
  ``sqlite_kb_store.SQLiteKnowledgeBaseStore``（真实写穿 + 启动回灌）**，
  降级事实由 ``factory.kb_persist_degraded()`` 暴露给 health（P3-STORE-4）。
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

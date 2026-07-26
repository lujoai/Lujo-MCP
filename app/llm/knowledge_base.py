"""按错误指纹存取历史分析结论的最小知识库模块。"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.llm.vector_store import get_vector_store

logger = logging.getLogger("ai-debug-mcp.knowledge-base")

DEFAULT_MAX_ENTRIES = 100
EVICTION_POLICY = "lru"


@dataclass(slots=True)
class KnowledgeBaseEntry:
    fingerprint: str
    analysis: dict[str, Any]
    fix_suggestion: str
    source: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "analysis": copy.deepcopy(self.analysis),
            "fix_suggestion": self.fix_suggestion,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class KnowledgeBaseStore:
    """基于进程内 OrderedDict 的最小知识库实现。"""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than 0")
        self.max_entries = max_entries
        self.eviction_policy = EVICTION_POLICY
        self._entries: "OrderedDict[str, KnowledgeBaseEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        if not fingerprint:
            return None

        with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return None
            self._entries.move_to_end(fingerprint)
            return entry.to_dict()

    def upsert(
        self,
        *,
        fingerprint: str,
        analysis: dict[str, Any],
        fix_suggestion: str,
        source: str,
    ) -> dict[str, Any]:
        if not fingerprint:
            raise ValueError("fingerprint is required")
        if not isinstance(analysis, dict):
            raise ValueError("analysis must be a dict")
        if not source:
            raise ValueError("source is required")

        now = time.time()
        with self._lock:
            existing = self._entries.get(fingerprint)
            created_at = existing.created_at if existing else now

            entry = KnowledgeBaseEntry(
                fingerprint=fingerprint,
                analysis=copy.deepcopy(analysis),
                fix_suggestion=fix_suggestion,
                source=source,
                created_at=created_at,
                updated_at=now,
            )
            self._entries[fingerprint] = entry
            self._entries.move_to_end(fingerprint)

            if existing is None and len(self._entries) > self.max_entries:
                evicted_fingerprint, _ = self._entries.popitem(last=False)
                logger.info(
                    "Knowledge base entry evicted",
                    extra={
                        "fingerprint": evicted_fingerprint,
                        "policy": self.eviction_policy,
                        "max_entries": self.max_entries,
                    },
                )

            return entry.to_dict()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


_knowledge_base = KnowledgeBaseStore()


def get_knowledge_base() -> KnowledgeBaseStore:
    return _knowledge_base


def get_knowledge_entry(fingerprint: str) -> dict[str, Any] | None:
    return _knowledge_base.get(fingerprint)


def upsert_knowledge_entry(
    *,
    fingerprint: str,
    analysis: dict[str, Any],
    fix_suggestion: str,
    source: str,
) -> dict[str, Any]:
    return _knowledge_base.upsert(
        fingerprint=fingerprint,
        analysis=analysis,
        fix_suggestion=fix_suggestion,
        source=source,
    )


def clear_knowledge_base() -> None:
    _knowledge_base.clear()


def retrieve_similar(query_text: str, top_k: int | None = None) -> list[dict[str, Any]]:
    """向量检索 fallback：精确指纹 miss 后按相似度召回历史分析。

    委托给当前 VectorStore 后端；vector store 关闭时（NullVectorStore）返回 []。
    调用方应在精确指纹匹配（get_knowledge_entry）miss 后调用本函数作为二级 fallback。

    Args:
        query_text: 查询文本（通常是当前调试上下文的 JSON 序列化结果）
        top_k: 召回数量；None 时使用 settings.vector_store_top_k

    Returns:
        list[dict]：相似 doc 列表，按相似度降序；无命中时返回 []
    """
    effective_top_k = top_k if top_k is not None else settings.vector_store_top_k
    pairs = get_vector_store().search(query_text, effective_top_k)
    return [doc for doc, _score in pairs]

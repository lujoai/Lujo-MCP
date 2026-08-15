"""按错误指纹存取历史分析结论的最小知识库模块。

v0.4.0 M2 增强：在原有精确指纹（L1）基础上，引入三级 fallback 匹配：
- L1 精确指纹：get(fingerprint)（原有）
- L1.5 归一化指纹：get_by_normalized_fingerprint（去变量值后的模式指纹）
- L2 类型级 Jaccard：get_by_type_fingerprint（同类型异常兜底）

并新增向量索引双写同步（_sync_entry_to_vector_store / _sync_all_to_vector_store），
保证 KB 写入后向量检索能覆盖全部条目，避免"写了但向量召不回"。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from app.config import settings
from app.rag.debug_case import (
    compute_normalized_fingerprint,
    compute_type_fingerprint,
)
from app.rag.vector_store import get_vector_store

logger = logging.getLogger("lujo-mcp.knowledge-base")

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
    # 三级 fallback 索引键（M2）
    normalized_fingerprint: str = ""
    type_fingerprint: str = ""
    # 验证统计（M4 Verify Loop 写回）
    verify_count: int = 0
    case_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "analysis": copy.deepcopy(self.analysis),
            "fix_suggestion": self.fix_suggestion,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "normalized_fingerprint": self.normalized_fingerprint,
            "type_fingerprint": self.type_fingerprint,
            "verify_count": self.verify_count,
            "case_confidence": self.case_confidence,
        }


def _extract_case_fields(analysis: dict[str, Any]) -> tuple[str, str]:
    """从 analysis 中提取异常类型与消息，用于归一化指纹计算。"""
    exc_type = str(analysis.get("exception_type") or "")
    message = str(analysis.get("message") or "")
    return exc_type, message


class KnowledgeBaseStore:
    """基于进程内 OrderedDict 的最小知识库实现（含三级 fallback 索引）。"""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than 0")
        self.max_entries = max_entries
        self.eviction_policy = EVICTION_POLICY
        self._entries: "OrderedDict[str, KnowledgeBaseEntry]" = OrderedDict()
        # 归一化指纹 → 精确指纹集合（L1.5）
        self._norm_index: dict[str, set[str]] = {}
        # 类型指纹 → 精确指纹集合（L2）
        self._type_index: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    # ── 精确指纹（L1）──

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        if not fingerprint:
            return None

        with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return None
            self._entries.move_to_end(fingerprint)
            return entry.to_dict()

    # ── 归一化指纹（L1.5）──

    def get_by_normalized_fingerprint(
        self, normalized_fingerprint: str
    ) -> dict[str, Any] | None:
        """按归一化指纹匹配（返回最近更新的条目）。"""
        if not normalized_fingerprint:
            return None
        with self._lock:
            candidates = self._norm_index.get(normalized_fingerprint)
            if not candidates:
                return None
            # 取最近更新的一条
            best = None
            for fp in candidates:
                entry = self._entries.get(fp)
                if entry is None:
                    continue
                if best is None or entry.updated_at > best.updated_at:
                    best = entry
            if best is None:
                return None
            self._entries.move_to_end(best.fingerprint)
            return best.to_dict()

    # ── 类型级（L2）──

    def get_by_type_fingerprint(
        self, type_fingerprint: str, top_k: int = 3
    ) -> list[dict[str, Any]]:
        """按类型指纹返回同类型历史条目（按更新时间倒序，最多 top_k 条）。"""
        if not type_fingerprint:
            return []
        with self._lock:
            candidates = self._type_index.get(type_fingerprint)
            if not candidates:
                return []
            entries = []
            for fp in candidates:
                entry = self._entries.get(fp)
                if entry is not None:
                    entries.append(entry)
            entries.sort(key=lambda e: e.updated_at, reverse=True)
            result = []
            for entry in entries[:top_k]:
                self._entries.move_to_end(entry.fingerprint)
                result.append(entry.to_dict())
            return result

    # ── 写入 / 淘汰 ──

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

        exc_type, message = _extract_case_fields(analysis)
        normalized_fp = compute_normalized_fingerprint(exc_type, message)
        type_fp = compute_type_fingerprint(exc_type)

        now = time.time()
        with self._lock:
            existing = self._entries.get(fingerprint)
            if existing is not None:
                # 更新旧索引
                self._remove_from_index(existing)
            created_at = existing.created_at if existing else now

            entry = KnowledgeBaseEntry(
                fingerprint=fingerprint,
                analysis=copy.deepcopy(analysis),
                fix_suggestion=fix_suggestion,
                source=source,
                created_at=created_at,
                updated_at=now,
                normalized_fingerprint=normalized_fp,
                type_fingerprint=type_fp,
            )
            self._entries[fingerprint] = entry
            self._entries.move_to_end(fingerprint)
            self._add_to_index(entry)

            if existing is None and len(self._entries) > self.max_entries:
                evicted_fingerprint, _ = self._entries.popitem(last=False)
                evicted = self._entries.get(evicted_fingerprint)
                if evicted is not None:
                    self._remove_from_index(evicted)
                logger.info(
                    "Knowledge base entry evicted",
                    extra={
                        "fingerprint": evicted_fingerprint,
                        "policy": self.eviction_policy,
                        "max_entries": self.max_entries,
                    },
                )

            result = entry.to_dict()

        # 向量双写同步（锁外执行，避免阻塞 IO；失败静默降级）
        if settings.kb_vector_index_autosync:
            self._sync_entry_to_vector_store(result)

        return result

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._norm_index.clear()
            self._type_index.clear()

    def record_verification(
        self, fingerprint: str, confidence: float
    ) -> Optional[dict[str, Any]]:
        """记录一次验证反馈：递增 verify_count，并提升 case_confidence。

        M4 Verify Loop 写回。按指纹精确命中条目后更新统计；未命中返回 None。
        更新后同步到向量库（幂等），失败静默降级。
        """
        if not fingerprint:
            return None
        with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None:
                return None
            entry.verify_count += 1
            entry.case_confidence = max(entry.case_confidence, float(confidence))
            entry.updated_at = time.time()
            result = entry.to_dict()

        if settings.kb_vector_index_autosync:
            self._sync_entry_to_vector_store(result)
        return result

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    # ── 索引维护 ──

    def _add_to_index(self, entry: KnowledgeBaseEntry) -> None:
        if entry.normalized_fingerprint:
            self._norm_index.setdefault(entry.normalized_fingerprint, set()).add(
                entry.fingerprint
            )
        if entry.type_fingerprint:
            self._type_index.setdefault(entry.type_fingerprint, set()).add(
                entry.fingerprint
            )

    def _remove_from_index(self, entry: KnowledgeBaseEntry) -> None:
        if entry.normalized_fingerprint:
            bucket = self._norm_index.get(entry.normalized_fingerprint)
            if bucket:
                bucket.discard(entry.fingerprint)
                if not bucket:
                    self._norm_index.pop(entry.normalized_fingerprint, None)
        if entry.type_fingerprint:
            bucket = self._type_index.get(entry.type_fingerprint)
            if bucket:
                bucket.discard(entry.fingerprint)
                if not bucket:
                    self._type_index.pop(entry.type_fingerprint, None)

    # ── 向量双写同步 ──

    def _sync_entry_to_vector_store(self, entry: dict[str, Any]) -> None:
        """把单条 KB entry 同步到向量库（幂等，失败静默降级）。"""
        try:
            get_vector_store().add([entry])
        except Exception:
            logger.warning(
                "KB→vector sync failed (fingerprint=%s)",
                entry.get("fingerprint"),
                exc_info=True,
            )

    def _sync_all_to_vector_store(self) -> None:
        """把当前全部 KB 条目同步到向量库（种子加载后重建向量索引）。"""
        with self._lock:
            entries = [e.to_dict() for e in self._entries.values()]
        if not entries:
            return
        try:
            get_vector_store().add(entries)
        except Exception:
            logger.warning("KB→vector full sync failed", exc_info=True)

    # ── 种子 / 批量导入 ──

    def load_seed_cases(self, cases: list[dict[str, Any]]) -> int:
        """批量导入种子知识（DebugCase.to_kb_entry 格式），返回导入条数。

        幂等：相同 fingerprint 覆盖更新；导入后自动重建向量索引（若开启）。
        """
        if not cases:
            return 0
        count = 0
        for case in cases:
            fingerprint = case.get("fingerprint")
            if not fingerprint:
                continue
            analysis = case.get("analysis") or {}
            self.upsert(
                fingerprint=fingerprint,
                analysis=analysis,
                fix_suggestion=case.get("fix_suggestion", ""),
                source=case.get("source", "seed"),
            )
            count += 1
        if settings.kb_vector_index_autosync:
            self._sync_all_to_vector_store()
        return count


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


def get_entry_by_normalized_fingerprint(
    normalized_fingerprint: str,
) -> dict[str, Any] | None:
    """L1.5 归一化指纹匹配（供 analyzer 三级 fallback 使用）。"""
    return _knowledge_base.get_by_normalized_fingerprint(normalized_fingerprint)


def get_entries_by_type_fingerprint(
    type_fingerprint: str, top_k: int = 3
) -> list[dict[str, Any]]:
    """L2 类型级匹配（供 analyzer 三级 fallback 使用）。"""
    return _knowledge_base.get_by_type_fingerprint(type_fingerprint, top_k=top_k)


def load_knowledge_base_seeds(cases: list[dict[str, Any]]) -> int:
    """加载种子知识到知识库。"""
    return _knowledge_base.load_seed_cases(cases)


def sync_knowledge_base_to_vector_store() -> None:
    """全量同步 KB → 向量库。"""
    _knowledge_base._sync_all_to_vector_store()


def record_verification(fingerprint: str, confidence: float) -> dict[str, Any] | None:
    """记录一次验证反馈（M4 Verify Loop 写回）。未命中返回 None。"""
    return _knowledge_base.record_verification(fingerprint, confidence)


def retrieve_similar(query_text: str, top_k: int | None = None) -> list[dict[str, Any]]:
    """向量检索 fallback：精确指纹 miss 后按相似度召回历史分析。

    委托给当前 VectorStore 后端；vector store 关闭时（NullVectorStore）返回 []。
    调用方应在精确指纹匹配（get_knowledge_entry）miss 后调用本函数作为 fallback。

    Args:
        query_text: 查询文本（通常是当前调试上下文的 JSON 序列化结果）
        top_k: 召回数量；None 时使用 settings.vector_store_top_k

    Returns:
        list[dict]：相似 doc 列表，按相似度降序；无命中时返回 []
    """
    effective_top_k = top_k if top_k is not None else settings.vector_store_top_k
    pairs = get_vector_store().search(query_text, effective_top_k)
    return [doc for doc, _score in pairs]
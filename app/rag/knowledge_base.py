"""按错误指纹存取历史分析结论的最小知识库模块。

v0.4.0 M2 增强：在原有精确指纹（L1）基础上，引入三级 fallback 匹配：
- L1 精确指纹：get(fingerprint)（原有）
- L1.5 归一化指纹：get_by_normalized_fingerprint（去变量值后的模式指纹）
- L2 类型级 Jaccard：get_by_type_fingerprint（同类型异常兜底）

并新增向量索引双写同步（_sync_entry_to_vector_store / _sync_all_to_vector_store），
保证 KB 写入后向量检索能覆盖全部条目，避免"写了但向量召不回"。

v0.5.3 新增 PG 持久化（写穿模式）：
- upsert / record_verification / LRU 驱逐 / clear 同步落库到 kb_entries 表
  （经 storage factory 分发，memory 后端 no-op，PG 故障 warning 降级不阻断）；
- load_from_persistent() 启动回灌：按 updated_at 倒序取最近 max_entries 条
  重建内存条目（含验证统计），跨重启保留 learned 知识。
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
from app.runtime.core.storage.factory import get_knowledge_store

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
        verify_count: int | None = None,
        case_confidence: float | None = None,
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
        evicted_fingerprint: str | None = None
        with self._lock:
            existing = self._entries.get(fingerprint)
            if existing is not None:
                # 更新旧索引
                self._remove_from_index(existing)
            created_at = existing.created_at if existing else now

            if verify_count is None:
                if existing is not None:
                    verify_count = existing.verify_count
                elif "verify_count" in analysis:
                    verify_count = int(analysis.get("verify_count", 0))
                else:
                    verify_count = 0

            if case_confidence is None:
                if existing is not None:
                    case_confidence = existing.case_confidence
                elif "case_confidence" in analysis:
                    case_confidence = float(analysis.get("case_confidence", 0.0))
                else:
                    case_confidence = 0.0

            entry = KnowledgeBaseEntry(
                fingerprint=fingerprint,
                analysis=copy.deepcopy(analysis),
                fix_suggestion=fix_suggestion,
                source=source,
                created_at=created_at,
                updated_at=now,
                normalized_fingerprint=normalized_fp,
                type_fingerprint=type_fp,
                verify_count=verify_count,
                case_confidence=case_confidence,
            )
            self._entries[fingerprint] = entry
            self._entries.move_to_end(fingerprint)
            self._add_to_index(entry)

            if existing is None and len(self._entries) > self.max_entries:
                evicted_fingerprint, evicted = self._entries.popitem(last=False)
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

        # PG 写穿持久化（锁外；LRU 驱逐同步删除，失败 warning 降级）
        self._persist_upsert(result, evicted_fingerprint)

        return result

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._norm_index.clear()
            self._type_index.clear()
        # PG 写穿：同步清空持久层（锁外执行）
        self._persist_clear()

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

        # PG 写穿：同步回写验证统计（锁外执行）
        self._persist_verification(result)
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

    # ── PG 写穿持久化（v0.5.3）──

    def _persistent_store(self):
        """获取持久化存储实例；不可用时返回 None（KB 退回纯内存行为）。"""
        try:
            return get_knowledge_store()
        except Exception:
            logger.warning("KB persistent store unavailable, falling back to memory-only",
                           exc_info=True)
            return None

    def _persist_upsert(
        self, entry: dict[str, Any], evicted_fingerprint: str | None
    ) -> None:
        """upsert 写穿落库 + LRU 驱逐同步删除（失败 warning 降级，不阻断主流程）。"""
        store = self._persistent_store()
        if store is None:
            return
        try:
            store.upsert_kb_entry(entry)
        except Exception:
            logger.warning(
                "KB→PG upsert failed (fingerprint=%s)",
                entry.get("fingerprint"),
                exc_info=True,
            )
        if evicted_fingerprint:
            try:
                store.delete_kb_entry(evicted_fingerprint)
            except Exception:
                logger.warning(
                    "KB→PG evict delete failed (fingerprint=%s)",
                    evicted_fingerprint,
                    exc_info=True,
                )

    def _persist_verification(self, entry: dict[str, Any]) -> None:
        """验证统计写穿回写（失败 warning 降级）。"""
        store = self._persistent_store()
        if store is None:
            return
        try:
            hit = store.update_kb_verification(
                entry.get("fingerprint", ""),
                entry.get("verify_count", 0),
                entry.get("case_confidence", 0.0),
                entry.get("updated_at", time.time()),
            )
            if not hit:
                # 记录在持久层缺失（如未持久化或冷启动），回退执行全量 upsert 保证经验不丢失
                logger.info(
                    "KB→PG verification update miss; falling back to upsert (fingerprint=%s)",
                    entry.get("fingerprint"),
                )
                store.upsert_kb_entry(entry)
        except Exception:
            logger.warning(
                "KB→PG verification update failed (fingerprint=%s)",
                entry.get("fingerprint"),
                exc_info=True,
            )

    def _persist_clear(self) -> None:
        """clear 写穿清空持久层（失败 warning 降级）。"""
        store = self._persistent_store()
        if store is None:
            return
        try:
            deleted = store.delete_all_kb_entries()
            logger.info("KB→PG cleared, deleted %d entries", deleted)
        except Exception:
            logger.warning("KB→PG clear failed", exc_info=True)

    def load_from_persistent(self) -> int:
        """启动回灌：从持久层加载最近 max_entries 条重建内存条目。

        - 按 updated_at 倒序取回，再按时间正序插入 OrderedDict（保证 LRU
          驱逐语义：最久未更新的条目位于队首）；
        - 保留 created_at/updated_at/verify_count/case_confidence 原值；
        - memory 后端（NoOp）返回 0，行为与历史版本一致；
        - 失败 warning 降级并返回 0，不阻断启动。
        """
        store = self._persistent_store()
        if store is None:
            return 0
        try:
            rows = store.list_recent_kb_entries(limit=self.max_entries)
        except Exception:
            logger.warning("KB startup load from persistent store failed", exc_info=True)
            return 0
        if not rows:
            return 0

        count = 0
        # 倒序取回 → 正序插入：updated_at 最旧的先插入（LRU 队首），最新的在队尾
        for row in reversed(rows):
            fingerprint = row.get("fingerprint")
            if not fingerprint:
                continue
            created_at = row.get("created_at") or time.time()
            updated_at = row.get("updated_at") or created_at
            with self._lock:
                entry = KnowledgeBaseEntry(
                    fingerprint=fingerprint,
                    analysis=copy.deepcopy(row.get("analysis") or {}),
                    fix_suggestion=row.get("fix_suggestion", "") or "",
                    source=row.get("source", "") or "",
                    created_at=created_at,
                    updated_at=updated_at,
                    normalized_fingerprint=row.get("normalized_fingerprint", "") or "",
                    type_fingerprint=row.get("type_fingerprint", "") or "",
                    verify_count=int(row.get("verify_count") or 0),
                    case_confidence=float(row.get("case_confidence") or 0.0),
                )
                # PG 为权威来源：直接覆盖内存中的同指纹条目（若有）
                existing = self._entries.get(fingerprint)
                if existing is not None:
                    self._remove_from_index(existing)
                self._entries[fingerprint] = entry
                self._entries.move_to_end(fingerprint)
                self._add_to_index(entry)
                count += 1

        logger.info(
            "KB startup load from persistent store: %d entries (max=%d)",
            count,
            self.max_entries,
        )
        return count

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
                verify_count=case.get("verify_count"),
                case_confidence=case.get("case_confidence"),
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
    verify_count: int | None = None,
    case_confidence: float | None = None,
) -> dict[str, Any]:
    return _knowledge_base.upsert(
        fingerprint=fingerprint,
        analysis=analysis,
        fix_suggestion=fix_suggestion,
        source=source,
        verify_count=verify_count,
        case_confidence=case_confidence,
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


def load_knowledge_base_from_persistent() -> int:
    """启动回灌：从持久层（kb_entries 表）加载最近条目回内存（v0.5.3）。"""
    return _knowledge_base.load_from_persistent()


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
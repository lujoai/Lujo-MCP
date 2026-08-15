"""Debug Experience Retriever —— 组合检索（P1 Debug Experience RAG）。

检索流程（三层，来源标记）：
1. fingerprint 精确匹配（source="fingerprint"，优先级最高）
2. message normalize 相似匹配（source="message_similarity"）
3. vector recall（仅 vector_store_enabled=True，source="vector"）

结果合并：fingerprint 去重 → score DESC → top_k。
任何异常静默降级：返回 [] 或已有成功结果，禁止 raise、不影响主流程。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.config import settings
from app.rag.debug_case import (
    compute_normalized_fingerprint,
    compute_type_fingerprint,
    tokenize_for_similarity,
)
from app.rag.experience import DebugExperienceRecord
from app.rag.knowledge_base import (
    get_entries_by_type_fingerprint,
    get_entry_by_normalized_fingerprint,
    get_knowledge_entry,
)
from app.rag.vector_store import get_vector_store

logger = logging.getLogger("lujo-mcp.rag.retriever")

# fingerprint 精确命中：优先级最高
_FINGERPRINT_SCORE = 1.0
# 归一化指纹精确命中（同模式、不同变量值）
_NORMALIZED_HIT_SCORE = 0.95
# 类型级候选放大系数（供 Jaccard 排序后截断）
_TYPE_CANDIDATE_MULTIPLIER = 3


def retrieve_debug_experience(
    exc_type: Optional[str] = None,
    message: Optional[str] = None,
    fingerprint: Optional[str] = None,
    debug_context: Optional[dict[str, Any]] = None,
    top_k: int = 3,
) -> list[DebugExperienceRecord]:
    """组合检索 Debug Experience。

    Args:
        exc_type: 异常类型（可选）
        message: 异常消息（可选）
        fingerprint: 精确指纹（可选，优先级最高）
        debug_context: build_debug_context 输出（可选，用于向量查询文本）
        top_k: 返回上限

    Returns:
        list[DebugExperienceRecord]：按分数降序；失败或空时返回 []。
    """
    if top_k <= 0:
        return []

    # 内部合并结构：fingerprint -> (record, score)，后写覆盖高分
    hits: dict[str, tuple[DebugExperienceRecord, float]] = {}

    try:
        _fingerprint_hits(hits, fingerprint)
    except Exception:
        logger.warning("debug experience: fingerprint retrieval failed", exc_info=True)

    try:
        _message_similarity_hits(hits, exc_type, message, top_k)
    except Exception:
        logger.warning("debug experience: message similarity failed", exc_info=True)

    try:
        _vector_hits(hits, debug_context, exc_type, message, top_k)
    except Exception:
        logger.warning("debug experience: vector recall failed", exc_info=True)

    if not hits:
        return []

    ranked = sorted(hits.values(), key=lambda kv: kv[1], reverse=True)
    return [rec for rec, _score in ranked[:top_k]]


# ── Step 1：fingerprint 精确匹配 ──


def _fingerprint_hits(
    hits: dict[str, tuple[DebugExperienceRecord, float]],
    fingerprint: Optional[str],
) -> None:
    if not fingerprint:
        return
    entry = get_knowledge_entry(fingerprint)
    if not entry:
        return
    rec = DebugExperienceRecord.from_kb_entry(entry)
    rec.source = "fingerprint"
    _add_hit(hits, rec, _FINGERPRINT_SCORE)


# ── Step 2：message normalize 相似匹配 ──


def _message_similarity_hits(
    hits: dict[str, tuple[DebugExperienceRecord, float]],
    exc_type: Optional[str],
    message: Optional[str],
    top_k: int,
) -> None:
    # L1.5：归一化指纹精确命中（同模式、不同变量值）
    normalized_fp = compute_normalized_fingerprint(exc_type, message)
    if normalized_fp:
        entry = get_entry_by_normalized_fingerprint(normalized_fp)
        if entry:
            rec = DebugExperienceRecord.from_kb_entry(entry)
            rec.source = "message_similarity"
            _add_hit(hits, rec, _NORMALIZED_HIT_SCORE)

    # L2：同类型候选 + Jaccard 相似度排序（不提前 return，保留多候选排序）
    type_fp = compute_type_fingerprint(exc_type)
    if not type_fp:
        return
    candidates = get_entries_by_type_fingerprint(
        type_fp, top_k=max(top_k * _TYPE_CANDIDATE_MULTIPLIER, 1)
    )
    if not candidates:
        return

    query_tokens = tokenize_for_similarity(message or "")
    if not query_tokens:
        return

    for entry in candidates:
        rec = DebugExperienceRecord.from_kb_entry(entry)
        entry_text = (entry.get("analysis") or {}).get("message") or ""
        entry_tokens = tokenize_for_similarity(entry_text)
        if not entry_tokens:
            continue
        inter = len(query_tokens & entry_tokens)
        union = len(query_tokens | entry_tokens)
        score = inter / union if union else 0.0
        # FIX: P2 debug_experience_min_score 接入 —— 此前预留配置从未被消费，
        # 低于阈值的 Jaccard 候选不返回（默认 0.0 = 仅过滤无重叠结果，保持原行为）
        if score <= settings.debug_experience_min_score:
            continue
        rec.source = "message_similarity"
        _add_hit(hits, rec, score)


# ── Step 3：vector recall（仅开启时）──


def _vector_hits(
    hits: dict[str, tuple[DebugExperienceRecord, float]],
    debug_context: Optional[dict[str, Any]],
    exc_type: Optional[str],
    message: Optional[str],
    top_k: int,
) -> None:
    if not settings.vector_store_enabled:
        return
    query = _build_vector_query(debug_context, exc_type, message)
    if not query:
        return
    pairs = get_vector_store().search(query, top_k)
    for doc, score in pairs:
        # FIX: P2 debug_experience_min_score 接入 —— 低于阈值的向量召回不返回
        if score <= settings.debug_experience_min_score:
            continue
        rec = DebugExperienceRecord.from_kb_entry(doc)
        rec.source = "vector"
        _add_hit(hits, rec, score)


# ── 合并辅助 ──


def _add_hit(
    hits: dict[str, tuple[DebugExperienceRecord, float]],
    rec: DebugExperienceRecord,
    score: float,
) -> None:
    """按 fingerprint 去重，保留更高分。"""
    key = rec.fingerprint or f"{rec.exception_type}|{rec.message_pattern}"
    prev = hits.get(key)
    if prev is None or score > prev[1]:
        hits[key] = (rec, score)


def _build_vector_query(
    debug_context: Optional[dict[str, Any]],
    exc_type: Optional[str],
    message: Optional[str],
) -> str:
    """向量查询文本：优先 debug_context JSON，其次异常特征文本。"""
    if debug_context:
        try:
            return json.dumps(debug_context, ensure_ascii=False, default=str)
        except Exception:
            pass
    return f"{exc_type or ''} {message or ''}".strip()

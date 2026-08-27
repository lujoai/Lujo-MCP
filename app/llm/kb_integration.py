"""知识库 / RAG 集成 —— 三级命中（精确/归一化/类型级）+ 向量召回 + 结果回写。

从 analyzer.py 拆出（god object 重构）：LLM 调用前的 KB 查找与
调用后的经验持久化，全部集中在分析仪（analyzer.py）之外的独立模块。
"""

import copy
import json
import logging
from typing import Optional

from app.config import settings
from app.rag.knowledge_base import (
    get_knowledge_entry,
    get_entry_by_normalized_fingerprint,
    get_entries_by_type_fingerprint,
    retrieve_similar_with_scores,
    upsert_knowledge_entry,
)
from app.rag.debug_case import (
    compute_normalized_fingerprint,
    compute_type_fingerprint,
    normalize_message_for_similarity,
)
from app.llm.context_prep import _get_error_signal

logger = logging.getLogger("lujo-mcp.llm")


def _get_knowledge_base_result(context: dict) -> Optional[dict]:
    exc_type, message, fingerprint = _get_error_signal(context)
    if not fingerprint:
        return None

    # L1：精确指纹命中
    entry = get_knowledge_entry(fingerprint)
    if entry is not None:
        return _build_kb_result(entry, "knowledge_base")

    # L1.5：归一化指纹命中（同模式、不同变量值）
    if exc_type or message:
        normalized_fp = compute_normalized_fingerprint(exc_type, message)
        entry = get_entry_by_normalized_fingerprint(normalized_fp)
        if entry is not None:
            return _build_kb_result(entry, "knowledge_base_normalized")

    # L2：类型级 Jaccard 兜底（同类型异常，消息 token 重叠）
    if settings.kb_type_level_fallback and exc_type:
        type_fp = compute_type_fingerprint(exc_type)
        candidates = get_entries_by_type_fingerprint(type_fp, top_k=5)
        entry = _best_type_fallback(candidates, message)
        if entry is not None:
            return _build_kb_result(entry, "knowledge_base_type")

    # 精确指纹 miss → 向量检索 RAG fallback（二级召回）
    return _try_vector_rag(context, fingerprint)


def _best_type_fallback(
    candidates: list[dict], message: str
) -> "dict | None":
    """在 L2 候选里按消息 Jaccard 相似度选最优（低于阈值返回 None）。"""
    if not candidates or not message:
        return None
    query_tokens = set(normalize_message_for_similarity(message).split())
    if not query_tokens:
        return None
    min_score = settings.kb_seed_jaccard_min_score
    best_entry: "dict | None" = None
    best_score = 0.0
    for cand in candidates:
        cand_msg = str(
            (cand.get("analysis") or {}).get("message")
            or (cand.get("_kb_meta") or {}).get("message")
            or ""
        )
        cand_tokens = set(normalize_message_for_similarity(cand_msg).split())
        if not cand_tokens:
            continue
        inter = len(query_tokens & cand_tokens)
        union = len(query_tokens | cand_tokens)
        score = inter / union if union else 0.0
        if score >= min_score and score > best_score:
            best_score = score
            best_entry = cand
    return best_entry


def _build_kb_result(entry: dict, analysis_source: str) -> dict:
    """把 KB entry 封装为 LLM 分析结果结构（与现有 knowledge_base 分支一致）。"""
    analysis = copy.deepcopy(entry.get("analysis") or {})
    fix_suggestion = entry.get("fix_suggestion")
    if fix_suggestion and not analysis.get("fix"):
        analysis["fix"] = fix_suggestion
    return {
        "analysis": analysis,
        "model": "__knowledge_base__",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "attempts": 0,
        "cached": False,
        "knowledge_base_hit": True,
        "analysis_source": analysis_source,
    }


def _try_vector_rag(context: dict, fingerprint: str) -> Optional[dict]:
    """向量检索 RAG fallback：精确指纹 miss 后按相似度召回历史分析。

    返回 None 表示无相似结果，调用方应继续走 LLM 链路。
    """
    try:
        query_text = json.dumps(context, ensure_ascii=False, default=str)
        similar_pairs = retrieve_similar_with_scores(query_text)
    except Exception:
        logger.warning("Vector retrieval failed", exc_info=True)
        return None
    if not similar_pairs:
        return None

    doc, score = similar_pairs[0]
    if score < settings.vector_store_min_score:
        logger.debug(
            "Vector RAG result below threshold (score=%.3f < %.3f), ignoring",
            score,
            settings.vector_store_min_score,
        )
        return None

    analysis = copy.deepcopy(doc.get("analysis") or {})
    fix_suggestion = doc.get("fix_suggestion") or analysis.get("fix")
    if fix_suggestion and not analysis.get("fix"):
        analysis["fix"] = fix_suggestion

    logger.info("Vector RAG hit (fingerprint=%s)", fingerprint)
    return {
        "analysis": analysis,
        "model": "__vector_rag__",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "attempts": 0,
        "cached": False,
        "knowledge_base_hit": False,
        "analysis_source": "vector_rag",
    }


def _persist_analysis_to_knowledge_base(
    fingerprint: Optional[str], result: dict, context: Optional[dict] = None
) -> None:
    if not fingerprint:
        return

    analysis = result.get("analysis")
    if not isinstance(analysis, dict):
        return

    # 注入异常类型/消息，支撑三级 fallback（L1.5 归一化 / L2 类型级）
    if context:
        exc_type, message, _ = _get_error_signal(context)
        persist_analysis = copy.deepcopy(analysis)
        persist_analysis.setdefault("exception_type", exc_type)
        persist_analysis.setdefault("message", message)
    else:
        persist_analysis = analysis

    try:
        upsert_knowledge_entry(
            fingerprint=fingerprint,
            analysis=persist_analysis,
            fix_suggestion=analysis.get("fix", ""),
            source="llm",
        )
    except Exception:
        logger.warning(
            "Knowledge base auto-persist failed (fingerprint=%s)",
            fingerprint,
            exc_info=True,
        )



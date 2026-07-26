from app.config import settings
from app.llm.knowledge_base import EVICTION_POLICY, KnowledgeBaseStore


# ---------------------------------------------------------------------------
# 原有：KnowledgeBaseStore 精确指纹存储测试
# ---------------------------------------------------------------------------


def test_upsert_adds_and_queries_entry():
    store = KnowledgeBaseStore(max_entries=2)

    entry = store.upsert(
        fingerprint="fp-1",
        analysis={"root_cause": "db timeout", "confidence": "high"},
        fix_suggestion="add retry",
        source="llm",
    )

    assert entry["fingerprint"] == "fp-1"
    assert entry["analysis"]["root_cause"] == "db timeout"
    assert entry["fix_suggestion"] == "add retry"
    assert entry["source"] == "llm"
    assert store.get("fp-1") == entry


def test_query_returns_none_for_missing_fingerprint():
    store = KnowledgeBaseStore(max_entries=2)

    assert store.get("missing-fp") is None


def test_upsert_updates_existing_entry_and_preserves_created_at():
    store = KnowledgeBaseStore(max_entries=2)

    original = store.upsert(
        fingerprint="fp-1",
        analysis={"root_cause": "old"},
        fix_suggestion="old fix",
        source="llm",
    )
    updated = store.upsert(
        fingerprint="fp-1",
        analysis={"root_cause": "new"},
        fix_suggestion="new fix",
        source="knowledge_base",
    )

    assert store.size() == 1
    assert updated["created_at"] == original["created_at"]
    assert updated["updated_at"] >= original["updated_at"]
    assert updated["analysis"]["root_cause"] == "new"
    assert updated["fix_suggestion"] == "new fix"
    assert updated["source"] == "knowledge_base"


def test_evicts_least_recently_used_entry_when_capacity_exceeded():
    store = KnowledgeBaseStore(max_entries=2)

    store.upsert(
        fingerprint="fp-1",
        analysis={"root_cause": "first"},
        fix_suggestion="fix 1",
        source="llm",
    )
    store.upsert(
        fingerprint="fp-2",
        analysis={"root_cause": "second"},
        fix_suggestion="fix 2",
        source="llm",
    )

    assert EVICTION_POLICY == "lru"
    assert store.get("fp-1") is not None

    store.upsert(
        fingerprint="fp-3",
        analysis={"root_cause": "third"},
        fix_suggestion="fix 3",
        source="llm",
    )

    assert store.get("fp-1") is not None
    assert store.get("fp-2") is None
    assert store.get("fp-3") is not None


# ---------------------------------------------------------------------------
# Phase 7：retrieve_similar 向量检索 fallback 测试
# ---------------------------------------------------------------------------


def test_retrieve_similar_returns_results_when_vector_store_has_docs(monkeypatch):
    """vector_store 有 doc 时 retrieve_similar 返回 doc 列表"""
    from app.llm.knowledge_base import retrieve_similar
    from app.llm.vector_store import InProcessVectorStore

    store = InProcessVectorStore()
    store.add([{
        "fingerprint": "fp-1",
        "analysis": {"root_cause": "database timeout problem"},
    }])
    monkeypatch.setattr("app.llm.knowledge_base.get_vector_store", lambda: store)

    results = retrieve_similar("database timeout problem")
    assert len(results) >= 1
    assert results[0]["fingerprint"] == "fp-1"


def test_retrieve_similar_returns_empty_when_vector_store_disabled(monkeypatch):
    """vector_store 关闭时（NullVectorStore）retrieve_similar 返回 []"""
    from app.llm.knowledge_base import retrieve_similar
    from app.llm.vector_store import NullVectorStore

    monkeypatch.setattr("app.llm.knowledge_base.get_vector_store", lambda: NullVectorStore())
    assert retrieve_similar("anything") == []


def test_retrieve_similar_uses_default_top_k_when_none(monkeypatch):
    """top_k=None 时使用 settings.vector_store_top_k 作为默认值"""
    from app.llm.knowledge_base import retrieve_similar
    from app.llm.vector_store import InProcessVectorStore

    store = InProcessVectorStore()
    for i in range(5):
        store.add([{
            "fingerprint": f"fp-{i}",
            "analysis": {"root_cause": "database timeout problem"},
        }])
    monkeypatch.setattr("app.llm.knowledge_base.get_vector_store", lambda: store)
    monkeypatch.setattr(settings, "vector_store_top_k", 2)

    results = retrieve_similar("database timeout problem")
    assert len(results) <= 2


def test_retrieve_similar_respects_explicit_top_k(monkeypatch):
    """显式 top_k 参数优先于 settings.vector_store_top_k"""
    from app.llm.knowledge_base import retrieve_similar
    from app.llm.vector_store import InProcessVectorStore

    store = InProcessVectorStore()
    for i in range(5):
        store.add([{
            "fingerprint": f"fp-{i}",
            "analysis": {"root_cause": "database timeout problem"},
        }])
    monkeypatch.setattr("app.llm.knowledge_base.get_vector_store", lambda: store)
    monkeypatch.setattr(settings, "vector_store_top_k", 10)

    results = retrieve_similar("database timeout problem", top_k=1)
    assert len(results) == 1

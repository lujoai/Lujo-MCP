from app.config import settings
from app.rag.knowledge_base import EVICTION_POLICY, KnowledgeBaseStore


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
    from app.rag.knowledge_base import retrieve_similar
    from app.rag.vector_store import InProcessVectorStore

    store = InProcessVectorStore()
    store.add([{
        "fingerprint": "fp-1",
        "analysis": {"root_cause": "database timeout problem"},
    }])
    monkeypatch.setattr("app.rag.knowledge_base.get_vector_store", lambda: store)

    results = retrieve_similar("database timeout problem")
    assert len(results) >= 1
    assert results[0]["fingerprint"] == "fp-1"


def test_retrieve_similar_returns_empty_when_vector_store_disabled(monkeypatch):
    """vector_store 关闭时（NullVectorStore）retrieve_similar 返回 []"""
    from app.rag.knowledge_base import retrieve_similar
    from app.rag.vector_store import NullVectorStore

    monkeypatch.setattr("app.rag.knowledge_base.get_vector_store", lambda: NullVectorStore())
    assert retrieve_similar("anything") == []


def test_retrieve_similar_uses_default_top_k_when_none(monkeypatch):
    """top_k=None 时使用 settings.vector_store_top_k 作为默认值"""
    from app.rag.knowledge_base import retrieve_similar
    from app.rag.vector_store import InProcessVectorStore

    store = InProcessVectorStore()
    for i in range(5):
        store.add([{
            "fingerprint": f"fp-{i}",
            "analysis": {"root_cause": "database timeout problem"},
        }])
    monkeypatch.setattr("app.rag.knowledge_base.get_vector_store", lambda: store)
    monkeypatch.setattr(settings, "vector_store_top_k", 2)

    results = retrieve_similar("database timeout problem")
    assert len(results) <= 2


def test_retrieve_similar_respects_explicit_top_k(monkeypatch):
    """显式 top_k 参数优先于 settings.vector_store_top_k"""
    from app.rag.knowledge_base import retrieve_similar
    from app.rag.vector_store import InProcessVectorStore

    store = InProcessVectorStore()
    for i in range(5):
        store.add([{
            "fingerprint": f"fp-{i}",
            "analysis": {"root_cause": "database timeout problem"},
        }])
    monkeypatch.setattr("app.rag.knowledge_base.get_vector_store", lambda: store)
    monkeypatch.setattr(settings, "vector_store_top_k", 10)

    results = retrieve_similar("database timeout problem", top_k=1)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# v0.4.0 M2：三级 fallback（L1 精确 / L1.5 归一化 / L2 类型级）+ 种子加载
# ---------------------------------------------------------------------------


def test_normalized_fingerprint_matching_l15(monkeypatch):
    """同模式不同变量值应命中归一化指纹（L1.5）"""
    from app.rag.debug_case import compute_normalized_fingerprint
    from app.rag.knowledge_base import KnowledgeBaseStore

    store = KnowledgeBaseStore()
    store.upsert(
        fingerprint="fp-exact",
        analysis={
            "root_cause": "int 解析失败",
            "exception_type": "ValueError",
            "message": "invalid literal for int() with base 10: 'abc'",
        },
        fix_suggestion="校验输入",
        source="seed",
    )
    # 用函数计算归一化指纹，避免硬编码不一致
    normalized = store.get_by_normalized_fingerprint(
        compute_normalized_fingerprint("ValueError", "invalid literal for int() with base 10: 'xyz'")
    )
    assert normalized is not None
    assert normalized["fingerprint"] == "fp-exact"


def test_type_fingerprint_matching_l2(monkeypatch):
    """同类型异常应可被类型级索引召回（L2）"""
    from app.rag.knowledge_base import KnowledgeBaseStore

    store = KnowledgeBaseStore()
    store.upsert(
        fingerprint="fp-keyerr",
        analysis={
            "root_cause": "缺少 user_id 键",
            "exception_type": "KeyError",
            "message": "'user_id'",
        },
        fix_suggestion="用 get()",
        source="seed",
    )
    candidates = store.get_by_type_fingerprint("keyerror")
    assert len(candidates) == 1
    assert candidates[0]["fingerprint"] == "fp-keyerr"


def test_type_fingerprint_strips_module_prefix():
    """类型指纹应剥离模块前缀，builtins.KeyError 与 KeyError 视为同类"""
    from app.rag.knowledge_base import KnowledgeBaseStore

    store = KnowledgeBaseStore()
    store.upsert(
        fingerprint="fp-qualified",
        analysis={
            "root_cause": "缺少键",
            "exception_type": "builtins.KeyError",
            "message": "'x'",
        },
        fix_suggestion="用 get()",
        source="seed",
    )
    candidates = store.get_by_type_fingerprint("keyerror")
    assert len(candidates) == 1


def test_load_seed_cases_populates_knowledge_base(monkeypatch):
    """种子知识应可批量导入，覆盖 45 条且各类别精确指纹可命中"""
    from app.rag.knowledge_base import KnowledgeBaseStore
    from app.rag.seed_data import SEED_CASES

    store = KnowledgeBaseStore()
    loaded = store.load_seed_cases(SEED_CASES)
    assert loaded == 45
    assert store.size() == 45
    # 精确指纹命中（L1）- 基础类型
    entry = store.get("seed:valueerror:int_literal")
    assert entry is not None
    assert entry["analysis"]["exception_type"] == "ValueError"
    # 精确指纹命中 - HTTP / Web
    http_entry = store.get("seed:http:502_bad_gateway")
    assert http_entry is not None
    assert http_entry["analysis"]["exception_type"] == "httpx.HTTPStatusError"
    # 精确指纹命中 - Frontend / Browser
    fe_entry = store.get("seed:frontend:cannot_read_map")
    assert fe_entry is not None
    assert fe_entry["analysis"]["exception_type"] == "TypeError"


def test_debug_case_roundtrip():
    """DebugCase 与 KB entry 应可往返（to_kb_entry / from_kb_entry）"""
    from app.rag.debug_case import DebugCase

    case = DebugCase(
        exception_type="ValueError",
        message="invalid literal for int() with base 10: 'abc'",
        fingerprint="fp-roundtrip",
        root_cause="输入校验缺失",
        fix_suggestion="加校验",
        tags=["valueerror"],
        case_confidence=0.9,
        verify_count=2,
    )
    entry = case.to_kb_entry()
    restored = DebugCase.from_kb_entry(entry)
    assert restored.exception_type == "ValueError"
    assert restored.case_confidence == 0.9
    assert restored.verify_count == 2
    assert restored.fingerprint == "fp-roundtrip"


def test_normalization_strips_noise():
    """归一化应剥离数字/hex/路径噪声，保留模式语义"""
    from app.rag.debug_case import normalize_message_for_similarity

    a = normalize_message_for_similarity("invalid literal for int() with base 10: 'abc'")
    b = normalize_message_for_similarity("invalid literal for int() with base 10: 'xyz'")
    assert a == b
    assert "12345" not in normalize_message_for_similarity("value 12345 is bad")

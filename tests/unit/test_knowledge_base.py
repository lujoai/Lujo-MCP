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


def test_persist_verification_hit_and_miss_fallback():
    """_persist_verification 在 update hit 时不 fallback，在 miss 时应 fallback 调用 upsert_kb_entry"""
    from unittest.mock import MagicMock
    from app.rag.knowledge_base import KnowledgeBaseStore

    kb = KnowledgeBaseStore()
    mock_store = MagicMock()
    kb._persistent_store = lambda: mock_store

    entry = {
        "fingerprint": "fp-test-fallback",
        "verify_count": 3,
        "case_confidence": 0.85,
        "updated_at": 1000.0,
    }

    # Case 1: Hit (hit=True) -> 不需要 fallback
    mock_store.update_kb_verification.return_value = True
    kb._persist_verification(entry)
    mock_store.update_kb_verification.assert_called_once_with("fp-test-fallback", 3, 0.85, 1000.0)
    mock_store.upsert_kb_entry.assert_not_called()

    # Case 2: Miss (hit=False) -> 必须 fallback 调用 upsert_kb_entry(entry)
    mock_store.reset_mock()
    mock_store.update_kb_verification.return_value = False
    kb._persist_verification(entry)
    mock_store.update_kb_verification.assert_called_once_with("fp-test-fallback", 3, 0.85, 1000.0)
    mock_store.upsert_kb_entry.assert_called_once_with(entry)

    # Case 3: Exception -> 不向外抛出异常，优雅降级
    mock_store.reset_mock()
    mock_store.update_kb_verification.side_effect = RuntimeError("db unavailable")
    kb._persist_verification(entry)  # 不应 raise


def test_load_seed_cases_preserves_case_confidence_and_verify_count():
    """FIX: 验证从 seed_data 载入种子条目时，顶层与 entry 对象的 case_confidence 保持预设值（0.8）"""
    kb = KnowledgeBaseStore()
    from app.rag.seed_data import SEED_CASES
    loaded = kb.load_seed_cases(SEED_CASES)
    assert loaded == len(SEED_CASES)

    for case in SEED_CASES:
        fp = case.get("fingerprint")
        entry = kb._entries.get(fp)
        assert entry is not None
        assert entry.case_confidence == 0.8
        assert entry.verify_count == 0

        d = kb.get(fp)
        assert d is not None
        assert d.get("case_confidence") == 0.8
        assert d.get("verify_count") == 0


def test_upsert_preserves_existing_verification_metrics():
    """FIX: 验证重新 upsert 已有条目时，若未显式传参则保留原先积累的 verify_count 与 case_confidence"""
    kb = KnowledgeBaseStore()
    fp = "test:upsert_metrics:001"
    kb.upsert(
        fingerprint=fp,
        analysis={"exception_type": "KeyError", "message": "missing key"},
        fix_suggestion="add key",
        source="test",
        verify_count=3,
        case_confidence=0.85,
    )

    entry1 = kb.get(fp)
    assert entry1["verify_count"] == 3
    assert entry1["case_confidence"] == 0.85

    # 重新 upsert 更新 fix_suggestion
    kb.upsert(
        fingerprint=fp,
        analysis={"exception_type": "KeyError", "message": "missing key modified"},
        fix_suggestion="new suggestion",
        source="test",
    )

    entry2 = kb.get(fp)
    assert entry2["verify_count"] == 3
    assert entry2["case_confidence"] == 0.85
    assert entry2["fix_suggestion"] == "new suggestion"


def test_record_verification_updates_lru_order():
    """验证 record_verification 会调用 move_to_end 更新 LRU 顺序。"""
    kb = KnowledgeBaseStore(max_entries=2)
    kb.upsert(
        fingerprint="fp-1",
        analysis={"exception_type": "ValueError", "message": "msg1"},
        fix_suggestion="fix1",
        source="test",
    )
    kb.upsert(
        fingerprint="fp-2",
        analysis={"exception_type": "ValueError", "message": "msg2"},
        fix_suggestion="fix2",
        source="test",
    )

    # 此时 LRU 顺序：fp-1 (最旧) -> fp-2 (最新)
    # 验证 fp-1，使其更新为最新使用
    res = kb.record_verification("fp-1", confidence=0.95)
    assert res is not None

    # 插入 fp-3，应驱逐最旧的 fp-2，保留 fp-1 和 fp-3
    kb.upsert(
        fingerprint="fp-3",
        analysis={"exception_type": "ValueError", "message": "msg3"},
        fix_suggestion="fix3",
        source="test",
    )

    assert kb.get("fp-1") is not None
    assert kb.get("fp-3") is not None
    assert kb.get("fp-2") is None


def test_compute_normalized_fingerprint_colon_handling():
    """验证 compute_normalized_fingerprint 使用 join，当类型为空但消息含冒号时不误剥离首尾冒号。"""
    from app.rag.debug_case import compute_normalized_fingerprint
    assert compute_normalized_fingerprint(None, ":some:value:") == ":some:value:"
    assert compute_normalized_fingerprint("ValueError", "bad input: 123") == "valueerror:bad input:"
    assert compute_normalized_fingerprint(None, "") == ""
    assert compute_normalized_fingerprint("", None) == ""


# ---------------------------------------------------------------------------
# R7-T4：KB LRU 驱逐 / clear 同步删除向量条目（向量索引只增不减修复）
# ---------------------------------------------------------------------------


class TestVectorIndexEvictionSync:
    def _make_store_with_vector(self, monkeypatch):
        """构造 KB store，其向量库替换为可观测的真实 InProcessVectorStore。"""
        from app.rag.vector_store import InProcessVectorStore

        vector_store = InProcessVectorStore(max_docs=100)
        monkeypatch.setattr(
            "app.rag.knowledge_base.get_vector_store", lambda: vector_store
        )
        monkeypatch.setattr(settings, "kb_vector_index_autosync", True)
        return vector_store

    def test_lru_eviction_deletes_vector_entry(self, monkeypatch):
        vector_store = self._make_store_with_vector(monkeypatch)
        store = KnowledgeBaseStore(max_entries=1)

        store.upsert(
            fingerprint="fp-1",
            analysis={"root_cause": "one"},
            fix_suggestion="f1",
            source="llm",
        )
        assert len(vector_store._docs) == 1

        # fp-2 触发 LRU 驱逐 fp-1 → 向量条目必须同步删除
        store.upsert(
            fingerprint="fp-2",
            analysis={"root_cause": "two"},
            fix_suggestion="f2",
            source="llm",
        )
        remaining = [doc.get("fingerprint") for _t, doc in vector_store._docs]
        assert remaining == ["fp-2"], "被驱逐条目的向量点不得残留（R7-T4）"

    def test_clear_deletes_all_vector_entries(self, monkeypatch):
        vector_store = self._make_store_with_vector(monkeypatch)
        store = KnowledgeBaseStore(max_entries=10)
        for fp in ("fp-1", "fp-2", "fp-3"):
            store.upsert(
                fingerprint=fp,
                analysis={"root_cause": fp},
                fix_suggestion="f",
                source="llm",
            )
        assert len(vector_store._docs) == 3

        store.clear()
        assert vector_store._docs == [], "clear 后向量库不得残留任何条目（R7-T4）"

    def test_autosync_off_keeps_legacy_behavior(self, monkeypatch):
        """kb_vector_index_autosync=False（种子加载路径）不触发向量写/删。"""
        vector_store = self._make_store_with_vector(monkeypatch)
        monkeypatch.setattr(settings, "kb_vector_index_autosync", False)
        store = KnowledgeBaseStore(max_entries=1)
        store.upsert(
            fingerprint="fp-1",
            analysis={"root_cause": "one"},
            fix_suggestion="f1",
            source="llm",
        )
        assert vector_store._docs == []

"""单元测试：向量检索 RAG 后端（Phase 7）"""
import pytest

from app.config import settings
from app.rag.vector_store import (
    InProcessVectorStore,
    NullVectorStore,
    VectorStore,
    _REGISTRY,
    _reset_vector_store,
    get_vector_store,
    register_vector_backend,
)


class TestInProcessVectorStore:
    def test_add_and_search_returns_relevant_docs(self):
        store = InProcessVectorStore()
        store.add([
            {"fingerprint": "fp-1", "analysis": {"root_cause": "database timeout problem"}},
            {"fingerprint": "fp-2", "analysis": {"root_cause": "disk full"}},
        ])
        results = store.search("database timeout problem", top_k=2)
        assert len(results) > 0
        doc, score = results[0]
        assert doc["fingerprint"] == "fp-1"
        assert score > 0

    def test_search_truncates_to_top_k(self):
        store = InProcessVectorStore()
        store.add([
            {"id": 1, "root_cause": "timeout error"},
            {"id": 2, "root_cause": "disk error"},
            {"id": 3, "root_cause": "memory error"},
        ])
        results = store.search("error", top_k=2)
        assert len(results) <= 2

    def test_search_filters_below_min_score(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_store_min_score", 0.99)
        store = InProcessVectorStore()
        store.add([{"root_cause": "totally unrelated xyz abc"}])
        results = store.search("database timeout problem", top_k=5)
        assert results == []

    def test_search_empty_store_returns_empty(self):
        store = InProcessVectorStore()
        assert store.search("anything", top_k=3) == []

    def test_search_empty_query_returns_empty(self):
        store = InProcessVectorStore()
        store.add([{"root_cause": "something"}])
        assert store.search("", top_k=3) == []

    def test_add_empty_list_is_noop(self):
        store = InProcessVectorStore()
        store.add([])
        assert store.search("anything", top_k=3) == []

    def test_fifo_eviction_when_over_max_docs(self):
        """R3：超过 max_docs 后按 FIFO 驱逐最旧 doc。"""
        store = InProcessVectorStore(max_docs=3)
        store.add([
            {"id": "a", "root_cause": "alpha"},
            {"id": "b", "root_cause": "bravo"},
            {"id": "c", "root_cause": "charlie"},
        ])
        # 第 4 条触发驱逐最旧的 a
        store.add([{"id": "d", "root_cause": "delta"}])
        assert store._max_docs == 3
        assert len(store._docs) == 3
        # 内部状态：a 已被驱逐，b/c/d 保留（FIFO）
        ids = [doc.get("id") for _text, doc in store._docs]
        assert "a" not in ids
        assert ids == ["b", "c", "d"]

    def test_max_docs_defaults_to_config(self, monkeypatch):
        """R3：max_docs 未显式传入时取配置 vector_store_max_docs。"""
        monkeypatch.setattr(settings, "vector_store_max_docs", 2)
        store = InProcessVectorStore()
        store.add([
            {"id": "a", "root_cause": "alpha"},
            {"id": "b", "root_cause": "bravo"},
            {"id": "c", "root_cause": "charlie"},
        ])
        assert store._max_docs == 2
        assert len(store._docs) == 2

    def test_search_results_sorted_desc_by_score(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_store_min_score", 0.0)
        store = InProcessVectorStore()
        store.add([
            {"id": "low", "root_cause": "error timeout"},
            {"id": "high", "root_cause": "error timeout disk full"},
        ])
        results = store.search("error timeout disk full", top_k=2)
        assert len(results) == 2
        # 降序：第一个 score 不小于第二个
        assert results[0][1] >= results[1][1]
        # high doc 与 query 重叠更多，应排第一
        assert results[0][0]["id"] == "high"


class TestNullVectorStore:
    def test_add_is_noop(self):
        store = NullVectorStore()
        store.add([{"any": "doc"}])  # 不应抛异常

    def test_search_returns_empty(self):
        store = NullVectorStore()
        assert store.search("anything", top_k=3) == []


class TestFactory:
    def setup_method(self):
        _reset_vector_store()

    def teardown_method(self):
        _reset_vector_store()

    def test_disabled_returns_null_vector_store(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_store_enabled", False)
        store = get_vector_store()
        assert isinstance(store, NullVectorStore)

    def test_in_process_backend_returns_in_process_store(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_store_enabled", True)
        monkeypatch.setattr(settings, "vector_store_backend", "in_process")
        store = get_vector_store()
        assert isinstance(store, InProcessVectorStore)

    def test_qdrant_backend_returns_qdrant_store(self, monkeypatch):
        """backend=qdrant 实例化 QdrantVectorStore；client 不可用时降级为 add=no-op / search=空。"""
        import app.rag.qdrant_vector_store as qdrant_module
        from app.rag.qdrant_vector_store import QdrantVectorStore

        monkeypatch.setattr(settings, "vector_store_enabled", True)
        monkeypatch.setattr(settings, "vector_store_backend", "qdrant")
        # mock 两个 client 返回 None，避免真实建连（验证降级行为）
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: None)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: None)
        store = get_vector_store()
        assert isinstance(store, QdrantVectorStore)
        # 降级行为：client 不可用时 search 返回空，add 不抛
        assert store.search("anything", 3) == []
        store.add([{"fingerprint": "x", "analysis": {}}])  # 不抛异常

    def test_unknown_backend_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_store_enabled", True)
        monkeypatch.setattr(settings, "vector_store_backend", "totally_unknown_backend")
        with pytest.raises(ValueError):
            get_vector_store()

    def test_singleton_caches_first_instance(self, monkeypatch):
        """单例：第二次调用返回同一实例"""
        monkeypatch.setattr(settings, "vector_store_enabled", True)
        monkeypatch.setattr(settings, "vector_store_backend", "in_process")
        store1 = get_vector_store()
        store2 = get_vector_store()
        assert store1 is store2


class TestRegistry:
    def test_register_vector_backend_adds_to_registry(self):
        class DummyStore(VectorStore):
            def add(self, docs): return None
            def search(self, query, top_k): return []

        try:
            register_vector_backend("dummy_test_backend", DummyStore)
            assert _REGISTRY["dummy_test_backend"] is DummyStore
        finally:
            _REGISTRY.pop("dummy_test_backend", None)

    def test_register_vector_backend_rejects_non_subclass(self):
        with pytest.raises(TypeError):
            register_vector_backend("bad_backend", object)  # type: ignore[arg-type]

    def test_factory_uses_registered_backend(self, monkeypatch):
        class DummyStore(VectorStore):
            def add(self, docs): return None
            def search(self, query, top_k): return []

        register_vector_backend("dummy_factory_test", DummyStore)
        try:
            monkeypatch.setattr(settings, "vector_store_enabled", True)
            monkeypatch.setattr(settings, "vector_store_backend", "dummy_factory_test")
            _reset_vector_store()
            store = get_vector_store()
            assert isinstance(store, DummyStore)
        finally:
            _REGISTRY.pop("dummy_factory_test", None)
            _reset_vector_store()

"""单元测试：Qdrant 向量检索适配器（Phase 7）。

覆盖：
- QdrantVectorStore.add / search 的成功路径与各降级路径
- _get_qdrant_client 的 collection 自动创建、维度不匹配降级
- _embed_texts 的批量分块、维度校验
- 工厂分派（backend=qdrant 实例化 QdrantVectorStore）

mock 策略：用 monkeypatch 替换 _get_qdrant_client / _get_embedding_client / _embed_texts，
避免真实网络调用。autouse fixture 每用例 reset 模块级单例 + 工厂单例。
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.rag import qdrant_vector_store as qdrant_module
from app.rag.qdrant_vector_store import QdrantVectorStore, _embed_texts, _get_qdrant_client
from app.rag.vector_store import _reset_vector_store


# ── 测试辅助：构造 fake embedding 响应 ──────────────────────────────


class _FakeEmbeddingItem:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_FakeEmbeddingItem(v) for v in vectors]


class _FakeHit:
    """模拟 qdrant ScoredPoint。"""

    def __init__(self, payload: dict, score: float) -> None:
        self.payload = payload
        self.score = score


def _make_vector(dim: int = 1536, val: float = 0.1) -> list[float]:
    return [val] * dim


def _make_embedding_client(vectors_per_call: list[list[float]] | None = None):
    """构造 fake OpenAI embedding client。

    Args:
        vectors_per_call: 每次 embeddings.create 返回的向量列表；None 表示抛异常
    """
    client = MagicMock()
    if vectors_per_call is None:
        client.embeddings.create.side_effect = RuntimeError("embed api down")
    else:
        client.embeddings.create.return_value = _FakeEmbeddingResponse(vectors_per_call)
    return client


def _make_qdrant_client():
    """构造 fake QdrantClient，默认 collection 存在且维度匹配。"""
    return MagicMock()


# ── autouse fixture：每用例 reset 单例 ──────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    qdrant_module._reset_qdrant_state()
    _reset_vector_store()
    yield
    qdrant_module._reset_qdrant_state()
    _reset_vector_store()


# ── A. add 路径 ────────────────────────────────────────────────────


class TestAdd:
    def test_add_empty_docs_is_noop(self):
        store = QdrantVectorStore()
        store.add([])  # 不抛、不调任何 client
        # 无断言异常即通过

    def test_add_qdrant_unavailable_is_noop(self, monkeypatch):
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: None)
        store = QdrantVectorStore()
        store.add([{"fingerprint": "fp1", "analysis": {}}])  # 不抛

    def test_add_embedding_unavailable_is_noop(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: None)
        store = QdrantVectorStore()
        store.add([{"fingerprint": "fp1", "analysis": {}}])
        mock_qdrant.upsert.assert_not_called()

    def test_add_embed_failure_is_noop(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: _make_embedding_client(None))
        store = QdrantVectorStore()
        store.add([{"fingerprint": "fp1", "analysis": {}}])
        mock_qdrant.upsert.assert_not_called()

    def test_add_upsert_failure_is_silent(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        mock_qdrant.upsert.side_effect = RuntimeError("qdrant write error")
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: MagicMock())
        vec = _make_vector()
        monkeypatch.setattr(qdrant_module, "_embed_texts", lambda texts: [vec] * len(texts))
        store = QdrantVectorStore()
        store.add([{"fingerprint": "fp1", "analysis": {}}])  # 不抛

    def test_add_success_calls_upsert_with_uuid5(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: MagicMock())
        vec = _make_vector()
        monkeypatch.setattr(qdrant_module, "_embed_texts", lambda texts: [vec] * len(texts))
        doc = {"fingerprint": "fp-abc", "analysis": {"root_cause": "x"}, "source": "llm"}
        store = QdrantVectorStore()
        store.add([doc])

        mock_qdrant.upsert.assert_called_once()
        call_kwargs = mock_qdrant.upsert.call_args.kwargs
        assert call_kwargs["collection_name"] == settings.qdrant_collection
        assert call_kwargs["wait"] is True
        points = call_kwargs["points"]
        assert len(points) == 1
        # point id 是 fingerprint 的 uuid5（确定性）
        expected_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "fp-abc"))
        assert str(points[0].id) == expected_id
        # payload 保留原始 doc 字段
        assert points[0].payload["fingerprint"] == "fp-abc"

    def test_add_missing_fingerprint_falls_back_to_uuid4(self, monkeypatch, caplog):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: MagicMock())
        vec = _make_vector()
        monkeypatch.setattr(qdrant_module, "_embed_texts", lambda texts: [vec] * len(texts))
        store = QdrantVectorStore()
        store.add([{"analysis": {"root_cause": "x"}}])  # 无 fingerprint

        mock_qdrant.upsert.assert_called_once()
        point_id = str(mock_qdrant.upsert.call_args.kwargs["points"][0].id)
        # uuid4 是随机的，无法预测具体值；验证它是合法 uuid 且非 uuid5(空串)
        parsed = uuid.UUID(point_id)
        assert parsed.version == 4

    def test_add_batch_chunking_respects_embed_batch(self, monkeypatch):
        """_embed_texts 按 2048 分块调用 embeddings.create。"""
        embed_client = _make_embedding_client([_make_vector()])
        # 每次 create 返回一个 chunk 的向量
        embed_client.embeddings.create.side_effect = [
            _FakeEmbeddingResponse([_make_vector()] * 2048),
            _FakeEmbeddingResponse([_make_vector()] * 1),
        ]
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: embed_client)
        vectors = _embed_texts(["t"] * 2049)
        assert len(vectors) == 2049
        assert embed_client.embeddings.create.call_count == 2


# ── B. search 路径 ─────────────────────────────────────────────────


class TestSearch:
    def test_search_empty_query_returns_empty(self):
        store = QdrantVectorStore()
        assert store.search("", 3) == []

    def test_search_zero_top_k_returns_empty(self):
        store = QdrantVectorStore()
        assert store.search("some query", 0) == []

    def test_search_negative_top_k_returns_empty(self):
        store = QdrantVectorStore()
        assert store.search("some query", -1) == []

    def test_search_qdrant_unavailable_returns_empty(self, monkeypatch):
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: None)
        store = QdrantVectorStore()
        assert store.search("query", 3) == []

    def test_search_embedding_unavailable_returns_empty(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: None)
        store = QdrantVectorStore()
        assert store.search("query", 3) == []

    def test_search_embed_failure_returns_empty(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: _make_embedding_client(None))
        store = QdrantVectorStore()
        assert store.search("query", 3) == []

    def test_search_success_returns_payload_and_score(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        hits = [
            _FakeHit({"fingerprint": "fp1", "analysis": {"root_cause": "db"}}, 0.92),
            _FakeHit({"fingerprint": "fp2", "analysis": {"root_cause": "net"}}, 0.81),
        ]
        mock_qdrant.search.return_value = hits
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: MagicMock())
        vec = _make_vector()
        monkeypatch.setattr(qdrant_module, "_embed_texts", lambda texts: [vec])
        store = QdrantVectorStore()
        results = store.search("query", 5)
        assert len(results) == 2
        assert results[0][0]["fingerprint"] == "fp1"
        assert results[0][1] == pytest.approx(0.92)
        assert results[1][0]["fingerprint"] == "fp2"
        assert results[1][1] == pytest.approx(0.81)

    def test_search_passes_score_threshold_and_limit(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        mock_qdrant.search.return_value = []
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: MagicMock())
        vec = _make_vector()
        monkeypatch.setattr(qdrant_module, "_embed_texts", lambda texts: [vec])
        store = QdrantVectorStore()
        store.search("query", 7)

        mock_qdrant.search.assert_called_once()
        call_kwargs = mock_qdrant.search.call_args.kwargs
        assert call_kwargs["collection_name"] == settings.qdrant_collection
        assert call_kwargs["limit"] == 7
        assert call_kwargs["score_threshold"] == settings.vector_store_min_score

    def test_search_failure_returns_empty(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        mock_qdrant.search.side_effect = RuntimeError("qdrant read error")
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: MagicMock())
        vec = _make_vector()
        monkeypatch.setattr(qdrant_module, "_embed_texts", lambda texts: [vec])
        store = QdrantVectorStore()
        assert store.search("query", 3) == []


# ── C. _get_qdrant_client 初始化逻辑 ──────────────────────────────


class TestQdrantClientInit:
    def test_collection_auto_create(self, monkeypatch):
        """collection 不存在时自动创建，维度与配置一致。"""
        from qdrant_client.models import Distance

        mock_client = MagicMock()
        mock_client.collection_exists.return_value = False
        monkeypatch.setattr(settings, "qdrant_embedding_dim", 1536)
        # 直接构造一个绕过单例的客户端构建（通过重新调用 _get_qdrant_client 逻辑）
        monkeypatch.setattr(
            "qdrant_client.QdrantClient", lambda **kwargs: mock_client
        )
        qdrant_module._reset_qdrant_state()
        result = _get_qdrant_client()
        assert result is mock_client
        mock_client.create_collection.assert_called_once()
        cc_kwargs = mock_client.create_collection.call_args.kwargs
        assert cc_kwargs["collection_name"] == settings.qdrant_collection
        vectors_config = cc_kwargs["vectors_config"]
        assert vectors_config.size == 1536
        assert vectors_config.distance == Distance.COSINE

    def test_collection_dim_mismatch_no_rebuild(self, monkeypatch, caplog):
        """collection 存在但维度不匹配时不重建，降级返回 None。"""
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        # 构造 fake CollectionInfo：实际维度 1024，配置期望 1536
        fake_info = MagicMock()
        fake_info.config.params.vectors.size = 1024
        mock_client.get_collection.return_value = fake_info
        monkeypatch.setattr(settings, "qdrant_embedding_dim", 1536)
        monkeypatch.setattr("qdrant_client.QdrantClient", lambda **kwargs: mock_client)
        qdrant_module._reset_qdrant_state()

        with caplog.at_level("WARNING"):
            result = _get_qdrant_client()
        assert result is None
        mock_client.create_collection.assert_not_called()
        # warning 含维度信息与恢复指引
        assert any("维度不匹配" in r.message for r in caplog.records)

    def test_collection_dim_match_returns_client(self, monkeypatch):
        """collection 存在且维度匹配时正常返回 client。"""
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        fake_info = MagicMock()
        fake_info.config.params.vectors.size = 1536
        mock_client.get_collection.return_value = fake_info
        monkeypatch.setattr(settings, "qdrant_embedding_dim", 1536)
        monkeypatch.setattr("qdrant_client.QdrantClient", lambda **kwargs: mock_client)
        qdrant_module._reset_qdrant_state()

        result = _get_qdrant_client()
        assert result is mock_client
        mock_client.create_collection.assert_not_called()


# ── D. _embed_texts 维度校验 ───────────────────────────────────────


class TestEmbedTexts:
    def test_embed_dim_mismatch_returns_none(self, monkeypatch, caplog):
        """返回向量维度与配置不一致时返回 None。"""
        embed_client = _make_embedding_client([_make_vector(dim=1024)])
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: embed_client)
        monkeypatch.setattr(settings, "qdrant_embedding_dim", 1536)

        with caplog.at_level("WARNING"):
            result = _embed_texts(["some text"])
        assert result is None
        assert any("维度不匹配" in r.message for r in caplog.records)

    def test_embed_empty_texts_returns_empty(self):
        assert _embed_texts([]) == []

    def test_redact_for_embedding_compound_keys_masked(self):
        """CR-2 回归：embedding 外发文本中的下划线复合敏感键必须脱敏。"""
        from app.rag.qdrant_vector_store import _redact_for_embedding

        out = _redact_for_embedding(
            "auth failed refresh_token=eyJsecret client_secret=cs-1"
        )
        assert "eyJsecret" not in out
        assert "cs-1" not in out
        assert 'refresh_token="***"' in out
        assert 'client_secret="***"' in out

        out2 = _redact_for_embedding('{"session_token":"st-1"}')
        assert "st-1" not in out2
        assert '"session_token":"***"' in out2

    def test_embed_client_unavailable_returns_none(self, monkeypatch):
        monkeypatch.setattr(qdrant_module, "_get_embedding_client", lambda: None)
        assert _embed_texts(["text"]) is None


# ── R7-T4：delete（KB 驱逐/清空同步删除向量点）────────────────────


class TestDelete:
    def test_delete_calls_client_with_uuid5_point_ids(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        store = QdrantVectorStore()

        store.delete(["fp-a", "fp-b"])

        mock_qdrant.delete.assert_called_once()
        kwargs = mock_qdrant.delete.call_args.kwargs
        assert kwargs["collection_name"] == settings.qdrant_collection
        assert kwargs["wait"] is True
        expected = [
            str(uuid.uuid5(uuid.NAMESPACE_DNS, "fp-a")),
            str(uuid.uuid5(uuid.NAMESPACE_DNS, "fp-b")),
        ]
        assert [str(p) for p in kwargs["points_selector"].points] == expected

    def test_delete_empty_list_is_noop(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        QdrantVectorStore().delete([])
        mock_qdrant.delete.assert_not_called()

    def test_delete_qdrant_unavailable_is_noop(self, monkeypatch):
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: None)
        QdrantVectorStore().delete(["fp-a"])  # 不抛异常

    def test_delete_failure_is_silent(self, monkeypatch):
        mock_qdrant = _make_qdrant_client()
        mock_qdrant.delete.side_effect = RuntimeError("qdrant down")
        monkeypatch.setattr(qdrant_module, "_get_qdrant_client", lambda: mock_qdrant)
        QdrantVectorStore().delete(["fp-a"])  # 静默降级，不抛异常

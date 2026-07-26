"""集成测试：Qdrant 向量检索真实读写链路。

目标：
- 验证 QdrantVectorStore.add / search 的端到端真实链路
- 验证 upsert 幂等性（同 fingerprint 重复写入不新增 point）
- 验证 score_threshold 过滤生效

说明：
- 这些测试依赖真实 Qdrant 服务 + OpenAI/智谱 API Key，默认环境下会 skip
- 推荐启动方式：``docker run -d -p 6333:6333 qdrant/qdrant``
- 需配置 OPENAI_API_KEY（或智谱）才能生成 embedding
"""

import socket
import uuid
from urllib.parse import urlparse

import pytest

from app.config import settings
from app.rag import qdrant_vector_store as qdrant_module
from app.rag.qdrant_vector_store import QdrantVectorStore
from app.rag.vector_store import _reset_vector_store


def _port_open(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


@pytest.fixture
def qdrant_url():
    return settings.qdrant_url or "http://127.0.0.1:6333"


@pytest.fixture
def require_qdrant(qdrant_url, monkeypatch):
    """探活 Qdrant；不可达时 skip 整个用例。同时需要 API Key 做 embedding。"""
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY 未配置，跳过 Qdrant 集成测试（无法生成 embedding）")

    parsed = urlparse(qdrant_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6333
    if not _port_open(host, port):
        pytest.skip("Qdrant 未启动，跳过 Qdrant 集成测试")

    from qdrant_client import QdrantClient

    client = QdrantClient(
        url=qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=settings.qdrant_request_timeout,
    )
    client.get_collections()  # 探活
    yield client


@pytest.fixture(autouse=True)
def _isolated_collection(require_qdrant, monkeypatch):
    """每用例独立 collection，避免互相污染；teardown 删除。"""
    collection_name = f"ai-debug-kb-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "qdrant_collection", collection_name)
    monkeypatch.setattr(settings, "vector_store_enabled", True)
    monkeypatch.setattr(settings, "vector_store_backend", "qdrant")
    monkeypatch.setattr(settings, "vector_store_min_score", 0.3)
    # 重置模块级单例，让本用例重新建连到独立 collection
    qdrant_module._reset_qdrant_state()
    _reset_vector_store()
    yield collection_name
    # 清理：删除测试用 collection
    try:
        require_qdrant.delete_collection(collection_name)
    except Exception:
        pass
    qdrant_module._reset_qdrant_state()
    _reset_vector_store()


@pytest.mark.integration
def test_qdrant_add_then_search_roundtrip():
    """写入 doc 后立即 search，应召回相似结果。"""
    store = QdrantVectorStore()
    store.add([
        {
            "fingerprint": "fp-timeout-001",
            "analysis": {"root_cause": "database connection timeout", "fix": "increase pool size"},
            "fix_suggestion": "increase pool size",
            "source": "llm",
        },
        {
            "fingerprint": "fp-disk-full-002",
            "analysis": {"root_cause": "disk space full", "fix": "clean logs"},
            "fix_suggestion": "clean logs",
            "source": "llm",
        },
    ])

    results = store.search("database connection timeout problem", top_k=2)
    assert len(results) > 0
    top_doc, top_score = results[0]
    assert top_doc["fingerprint"] == "fp-timeout-001"
    assert top_score > 0


@pytest.mark.integration
def test_qdrant_search_no_match_returns_empty(monkeypatch):
    """写入 doc 后查询完全不相关的内容，应返回空（受 score_threshold 过滤）。"""
    store = QdrantVectorStore()
    store.add([
        {
            "fingerprint": "fp-specific-003",
            "analysis": {"root_cause": "database timeout", "fix": "retry"},
            "fix_suggestion": "retry",
            "source": "llm",
        },
    ])
    # 调高阈值，确保不相关查询被过滤
    monkeypatch.setattr(settings, "vector_store_min_score", 0.99)
    results = store.search("completely unrelated topic about weather and cooking", top_k=3)
    assert results == []


@pytest.mark.integration
def test_qdrant_upsert_idempotent(require_qdrant):
    """同 fingerprint 写两次，collection 内 point 数量不变（upsert 覆盖）。"""
    collection_name = settings.qdrant_collection
    store = QdrantVectorStore()
    doc = {
        "fingerprint": "fp-idempotent-004",
        "analysis": {"root_cause": "memory leak", "fix": "fix ref cycle"},
        "fix_suggestion": "fix ref cycle",
        "source": "llm",
    }

    store.add([doc])
    info_after_first = require_qdrant.get_collection(collection_name)
    count_after_first = info_after_first.points_count or 0

    store.add([doc])  # 同 fingerprint 再写一次
    info_after_second = require_qdrant.get_collection(collection_name)
    count_after_second = info_after_second.points_count or 0

    assert count_after_first == count_after_second == 1


@pytest.mark.integration
def test_qdrant_score_threshold_filters_low_score(monkeypatch):
    """降低阈值能召回更多，升高阈值召回更少。"""
    store = QdrantVectorStore()
    store.add([
        {
            "fingerprint": "fp-threshold-005",
            "analysis": {"root_cause": "network timeout error", "fix": "increase timeout"},
            "fix_suggestion": "increase timeout",
            "source": "llm",
        },
    ])

    # 低阈值应能召回
    monkeypatch.setattr(settings, "vector_store_min_score", 0.0)
    low_results = store.search("network timeout error", top_k=3)
    assert len(low_results) > 0

    # 极高阈值应过滤掉
    monkeypatch.setattr(settings, "vector_store_min_score", 0.99)
    high_results = store.search("network timeout error", top_k=3)
    assert high_results == []

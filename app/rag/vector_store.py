"""向量检索 RAG 的检索语义抽象层（Phase 7）。

接口约束：抽象落在 add(docs) / search(query, top_k) 检索语义；
禁止 Qdrant collection / point / vector_id 等底层概念 leak 进接口。

后端：
- in_process（默认，零外部依赖）：Jaccard token 重叠相似度
- qdrant：本轮留空插槽，配置即显式 raise NotImplementedError
- 未来可通过 register_vector_backend 注册更多后端
"""

from __future__ import annotations

import json
import logging
import re
import threading
from abc import ABC, abstractmethod
from typing import Any

from app.config import settings

logger = logging.getLogger("ai-debug-mcp.vector-store")

# 非字母数字字符作为 token 分隔符（ASCII 字母数字被保留，其余字符作分隔）
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z]+")


def _tokenize(text: str) -> set[str]:
    """按非字母数字切分为 token，返回去重 set（用于 Jaccard 相似度）。"""
    if not text:
        return set()
    return {tok for tok in _TOKEN_SPLIT.split(text.lower()) if tok}


def _serialize_doc(doc: dict[str, Any]) -> str:
    """把分析 doc 序列化为可比较的纯文本。"""
    return json.dumps(doc, ensure_ascii=False, default=str)


class VectorStore(ABC):
    """检索语义抽象层：add(docs) 写入；search(query, top_k) 召回。"""

    @abstractmethod
    def add(self, docs: list[dict[str, Any]]) -> None:
        """写入若干 doc（dict 形式，内部自行序列化为可比较文本）。"""

    @abstractmethod
    def search(self, query: str, top_k: int) -> list[tuple[dict[str, Any], float]]:
        """召回 top_k 个 (doc, score) 对，按 score 降序。"""


class NullVectorStore(VectorStore):
    """feature 关闭时的 no-op 实现：add 不做事，search 返回 []。

    让调用方可以无条件调用 add/search，无需在调用点判断开关。
    """

    def add(self, docs: list[dict[str, Any]]) -> None:
        return None

    def search(self, query: str, top_k: int) -> list[tuple[dict[str, Any], float]]:
        return []


class InProcessVectorStore(VectorStore):
    """进程内零依赖实现。

    用 token 重叠的 Jaccard 相似度（零 numpy / sentence-transformers 依赖）。
    每条 doc 存 (text, original_doc) 与对应 token set；search 时计算 query token
    与每条 doc token 的 Jaccard 相似度，过滤 min_score 后取 top_k。

    容量上限：超过 ``max_docs`` 时按 FIFO 淘汰最旧 doc，避免长期运行 OOM
    （对齐 MemoryTraceStore 的容量约束，CODE_REVIEW R3）。
    """

    def __init__(self, max_docs: int | None = None) -> None:
        # max_docs 为 None 时取配置（默认 10000），保证单例与测试注入均可控
        if max_docs is None:
            max_docs = int(getattr(settings, "vector_store_max_docs", 10000))
        self._max_docs = max(1, max_docs)
        self._docs: list[tuple[str, dict[str, Any]]] = []
        self._doc_tokens: list[set[str]] = []
        self._lock = threading.Lock()

    def add(self, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        with self._lock:
            for doc in docs:
                text = _serialize_doc(doc)
                self._docs.append((text, doc))
                self._doc_tokens.append(_tokenize(text))
            # FIFO 驱逐最旧 doc，直至容量以内（长期运行内存有界）
            overflow = len(self._docs) - self._max_docs
            if overflow > 0:
                del self._docs[:overflow]
                del self._doc_tokens[:overflow]

    def search(self, query: str, top_k: int) -> list[tuple[dict[str, Any], float]]:
        if not query or top_k <= 0:
            return []
        with self._lock:
            snapshot = list(zip(self._docs, self._doc_tokens))
        if not snapshot:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        min_score = settings.vector_store_min_score
        scored: list[tuple[dict[str, Any], float]] = []
        for (_text, doc), doc_tokens in snapshot:
            if not doc_tokens:
                continue
            inter = len(query_tokens & doc_tokens)
            if inter == 0:
                continue
            union = len(query_tokens | doc_tokens)
            score = inter / union if union else 0.0
            if score >= min_score:
                scored.append((doc, score))

        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:top_k]


# ── Backend 注册表与工厂 ────────────────────────────────────────────

_REGISTRY: dict[str, type[VectorStore]] = {"in_process": InProcessVectorStore}


def register_vector_backend(name: str, cls: type[VectorStore]) -> None:
    """注册新的向量检索后端（如未来 Qdrant 适配器）。

    保留名 "in_process" / "qdrant" 由工厂直接处理，注册同名后端不会生效。
    """
    if not name:
        raise ValueError("backend name is required")
    if not isinstance(cls, type) or not issubclass(cls, VectorStore):
        raise TypeError("cls must be a VectorStore subclass")
    _REGISTRY[name] = cls


_vector_store: VectorStore | None = None
_vector_store_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    """单例工厂：根据 settings 返回合适的 VectorStore。

    - 关闭时返回 NullVectorStore，调用方可无条件调用 add/search
    - backend=in_process：返回 InProcessVectorStore
    - backend=qdrant：实例化 QdrantVectorStore（依赖未装/连接失败时静默降级为 add=no-op / search=空）
    - 其他已注册后端：实例化 _REGISTRY[name]
    - 未知后端：ValueError
    """
    global _vector_store
    if _vector_store is not None:
        return _vector_store
    with _vector_store_lock:
        if _vector_store is None:
            _vector_store = _build_vector_store()
    return _vector_store


def _build_vector_store() -> VectorStore:
    if not settings.vector_store_enabled:
        return NullVectorStore()
    backend = settings.vector_store_backend
    if backend == "in_process":
        return InProcessVectorStore()
    if backend == "qdrant":
        # 函数内导入：破循环（qdrant_vector_store 会 import 本模块的 VectorStore/_serialize_doc）
        # + 可选依赖隔离（未装 qdrant-client 时仅在显式配置 backend=qdrant 才触发降级）
        from app.rag.qdrant_vector_store import QdrantVectorStore
        return QdrantVectorStore()
    cls = _REGISTRY.get(backend)
    if cls is None:
        raise ValueError(f"Unknown vector store backend: {backend}")
    return cls()


def _reset_vector_store() -> None:
    """测试辅助：重置单例（仅供单测使用，生产代码不应调用）。"""
    global _vector_store
    with _vector_store_lock:
        _vector_store = None

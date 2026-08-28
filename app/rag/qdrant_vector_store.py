"""Qdrant 向量检索适配器（Phase 7）。

实现 ``VectorStore`` ABC，接入 OpenAI/智谱 Embeddings API 做真正的语义召回。
所有 Qdrant 的 collection/point/vector_id 概念封装在本模块内部，不 leak 进接口。

设计约束：
- fail-safe：Qdrant/embedding 不可用时 ``add``=no-op + warning，``search`` 返回 ``[]``，
  绝不抛异常穿透到 LLM 主链路（参照 ``analyzer._get_redis_cache`` 的降级模式）。
- embedding client 独立于 ``analyzer._get_client``：解耦 + 错误语义不同
  （analyzer 缺 API Key 直接 raise，本模块须静默降级）。
- provider 分派常量在模块内复制一份，避免 ``analyzer → vector_store → analyzer`` 循环 import。
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from typing import Any, Optional

from app.config import settings
from app.rag.vector_store import VectorStore, _serialize_doc

logger = logging.getLogger("lujo-mcp.qdrant-vector-store")

# OpenAI embeddings API 单次最多 2048 个 input
_EMBED_BATCH_SIZE = 2048

# provider → 默认 base_url（与 analyzer._PROVIDER_BASE_URLS 保持一致，复制以避免循环 import）
_PROVIDER_BASE_URLS = {
    "openai": "",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/",
    "deepseek": "https://api.deepseek.com",
    "custom": "",
}

# ── 脱敏正则（与 app/runtime/core/redaction.py 的 _DEFAULT_RULES 保持一致）────
# 架构冻结禁止 rag → runtime import，故此处内联复制（与 _PROVIDER_BASE_URLS 同样手法）。
# embedding 把文档原文发往外部 LLM 服务，必须先脱敏，否则密钥/token/手机号会外发。
# FIX: CR-2 —— 键名匹配改为"包含敏感词干"语义，覆盖 refresh_token / client_secret
# 等下划线复合键（\b 词边界在 '_' 处不成立导致此前漏脱敏），与 redaction.py 同步。
_SENSITIVE_KEY_NAME = (
    r"[\w.-]*(?:password|passwd|pwd|secret|token|apikey|credential|private[_-]?key)[\w.-]*"
    r"|[\w.-]*[_-]key"
)
_REDACT_RULES: list[tuple[re.Pattern[str], str]] = [
    # password = "x", pwd: xxx, refresh_token=eyJ..., client_secret=xxx ...
    (
        re.compile(
            r"(?i)\b(" + _SENSITIVE_KEY_NAME + r")\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|\S+)"
        ),
        r'\1="***"',
    ),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+))(?:'[^']*'|\"[^\"]*\"|\S+)"), r"\1***"),
    # JSON 格式: {"password":"xxx"}, {"refresh_token":"xxx"}, {"api_key":"xxx"} ...
    (
        re.compile(
            r"(?i)\"(" + _SENSITIVE_KEY_NAME + r"|authorization)\"\s*:\s*(?:'[^']*'|\"[^\"]*\"|\S+)"
        ),
        r'"\1":"***"',
    ),
    # 中国大陆 11 位手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***PHONE***"),
]


def _redact_for_embedding(text: str) -> str:
    """embedding 外发前的脱敏。

    - settings.redaction_enabled=False：原样返回（与 runtime/core/redaction.py 语义一致，
      但 embedding 路径默认仍脱敏；仅在用户显式关闭全局脱敏时才不脱敏）。
    - None / 非字符串 / 空串：原样返回。
    """
    if not isinstance(text, str) or not text:
        return text
    if not settings.redaction_enabled:
        logger.warning("redaction disabled — embedding 外发可能包含敏感数据")
        return text
    for pattern, repl in _REDACT_RULES:
        text = pattern.sub(repl, text)
    return text

# ── 模块级单例（参照 analyzer._get_redis_cache 的双重检查锁模式）────────────
_qdrant_client: Optional[Any] = None  # QdrantClient | None
# collection 是否已就绪（含已尝试建连/建表）。True 后不再重试，避免故障期每次调用都打网络。
_qdrant_collection_ready: bool = False
_qdrant_lock = threading.Lock()

_embedding_client: Optional[Any] = None  # OpenAI | None
_embedding_client_lock = threading.Lock()


def _resolve_embedding_base_url() -> str:
    """确定 embedding client 的 base_url：显式配置优先 → provider 默认 → 空。"""
    return settings.llm_base_url or _PROVIDER_BASE_URLS.get(settings.llm_provider, "")


def _get_qdrant_client() -> Optional[Any]:
    """惰性获取 Qdrant 客户端，不可用时返回 None。

    流程：
    1. ``_qdrant_collection_ready=True`` 时无锁快速返回（含 None 降级态）。
    2. 双重检查锁内：import qdrant_client（失败说明依赖未装）→ 建连 → 探活 collection：
       - 不存在 → 自动 create_collection（维度=settings.qdrant_embedding_dim, COSINE）
       - 存在但维度不匹配 → 不自动重建（丢数据）→ warning + None
    3. 任何异常 → ``_qdrant_client=None`` + ``_qdrant_collection_ready=True``。
    """
    global _qdrant_client, _qdrant_collection_ready
    if _qdrant_collection_ready:
        return _qdrant_client
    with _qdrant_lock:
        if _qdrant_collection_ready:
            return _qdrant_client
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http.exceptions import UnexpectedResponse
        except ImportError:
            logger.warning(
                "qdrant-client 未安装，Qdrant 向量检索已禁用；"
                "pip install qdrant-client>=1.9.0 后重启生效"
            )
            _qdrant_collection_ready = True
            return None

        try:
            client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key or None,
                timeout=settings.qdrant_request_timeout,
            )
            collection_name = settings.qdrant_collection
            expected_dim = settings.qdrant_embedding_dim

            if not client.collection_exists(collection_name):
                # 首次使用：自动创建 collection
                from qdrant_client.models import Distance, VectorParams
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=VectorParams(
                        size=expected_dim,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info(
                    "Qdrant collection auto-created: name=%s dim=%d",
                    collection_name,
                    expected_dim,
                )
            else:
                # collection 已存在：校验维度，不匹配则降级（不自动重建避免丢数据）
                info = client.get_collection(collection_name)
                vectors_config = info.config.params.vectors
                actual_dim = getattr(vectors_config, "size", None)
                if actual_dim is None and isinstance(vectors_config, dict):
                    # NamedVectors 场景：取第一个向量配置的 size
                    first = next(iter(vectors_config.values()), None)
                    actual_dim = getattr(first, "size", None)
                if actual_dim is not None and actual_dim != expected_dim:
                    logger.warning(
                        "Qdrant collection 维度不匹配: collection=%s actual=%d expected=%d；"
                        "不自动重建（避免丢数据）。请删除 collection 后重启，"
                        "或修改 QDRANT_EMBEDDING_DIM 与现有 collection 对齐",
                        collection_name,
                        actual_dim,
                        expected_dim,
                    )
                    _qdrant_client = None
                    _qdrant_collection_ready = True
                    return None

            _qdrant_client = client
            logger.info("Qdrant 客户端已连接: url=%s collection=%s", settings.qdrant_url, collection_name)
        except UnexpectedResponse as e:
            logger.warning("Qdrant 连接异常 (status=%s): %s", getattr(e, "status_code", "?"), e)
            _qdrant_client = None
        except Exception:
            logger.warning("Qdrant 客户端初始化失败，向量检索降级为 no-op", exc_info=True)
            _qdrant_client = None
        finally:
            _qdrant_collection_ready = True
    return _qdrant_client


def _get_embedding_client() -> Optional[Any]:
    """惰性获取独立 OpenAI 客户端（用于 embeddings），不可用时返回 None。

    独立于 ``analyzer._get_client``：
    - 解耦：未来 LLM 与 embedding 可拆不同 provider
    - 错误语义不同：analyzer 缺 API Key 直接 raise，本模块须静默降级
    """
    global _embedding_client
    if _embedding_client is not None:
        return _embedding_client
    with _embedding_client_lock:
        if _embedding_client is not None:
            return _embedding_client
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("openai 未安装，embedding 不可用")
            return None

        api_key = settings.openai_api_key
        if not api_key:
            logger.warning(
                "OPENAI_API_KEY 未配置，Qdrant embedding 降级为 no-op；"
                "配置后重启生效"
            )
            return None

        base_url = _resolve_embedding_base_url()
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": settings.llm_timeout,
            "max_retries": 0,  # 失败即降级，不重试拖慢主链路
        }
        if base_url:
            kwargs["base_url"] = base_url
        try:
            _embedding_client = OpenAI(**kwargs)
        except Exception:
            logger.warning("OpenAI embedding 客户端初始化失败", exc_info=True)
            return None
    return _embedding_client


def _embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """批量生成 embedding，失败返回 None。

    按 ``_EMBED_BATCH_SIZE``(2048) 分块调用 OpenAI embeddings API。
    维度校验：返回向量维度须与 ``settings.qdrant_embedding_dim`` 一致，否则 None。
    任何异常 → None（调用方静默降级）。
    """
    if not texts:
        return []
    client = _get_embedding_client()
    if client is None:
        return None

    # SEC: 外发前脱敏，防止密钥/token/手机号等敏感数据流向外部 embedding API
    texts = [_redact_for_embedding(t) for t in texts]

    all_vectors: list[list[float]] = []
    expected_dim = settings.qdrant_embedding_dim
    try:
        for i in range(0, len(texts), _EMBED_BATCH_SIZE):
            chunk = texts[i : i + _EMBED_BATCH_SIZE]
            response = client.embeddings.create(
                model=settings.qdrant_embedding_model,
                input=chunk,
            )
            for item in response.data:
                vec = item.embedding
                if len(vec) != expected_dim:
                    logger.warning(
                        "embedding 维度不匹配: actual=%d expected=%d model=%s；"
                        "请核对 QDRANT_EMBEDDING_MODEL 与 QDRANT_EMBEDDING_DIM",
                        len(vec),
                        expected_dim,
                        settings.qdrant_embedding_model,
                    )
                    return None
                all_vectors.append(vec)
    except Exception:
        logger.warning("embedding 调用失败，向量检索降级", exc_info=True)
        return None
    return all_vectors


class QdrantVectorStore(VectorStore):
    """Qdrant 后端实现：用 OpenAI/智谱 Embeddings 做语义召回。

    Qdrant 概念（collection/point/vector_id）全部封装在本类内部，对外只暴露
    ``add(docs)`` / ``search(query, top_k)`` 检索语义。

    降级矩阵：
    - qdrant-client 未装 / Qdrant 连不上 / collection 维度不匹配 → add=no-op, search=[]
    - OpenAI client 初始化失败 / embedding API 失败 / 维度校验失败 → 同上
    - upsert / search 网络失败 → 同上
    """

    def add(self, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        client = _get_qdrant_client()
        if client is None:
            return
        if _get_embedding_client() is None:
            return

        texts = [_serialize_doc(doc) for doc in docs]
        vectors = _embed_texts(texts)
        if vectors is None:
            return

        try:
            from qdrant_client.models import PointStruct
        except ImportError:
            logger.warning("qdrant-client 未安装，upsert 跳过")
            return

        points: list[PointStruct] = []
        for doc, vec in zip(docs, vectors):
            fingerprint = doc.get("fingerprint")
            if fingerprint:
                # 确定性 point id：同 fingerprint 重新分析会覆盖而非新增（幂等 upsert）
                point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, str(fingerprint)))
            else:
                # fingerprint 缺失兜底：uuid4 随机 id（不幂等，但避免 Qdrant 拒收）
                logger.warning("doc missing fingerprint, falling back to uuid4")
                point_id = str(uuid.uuid4())
            points.append(
                PointStruct(
                    id=point_id,
                    vector=vec,
                    payload=doc,
                )
            )

        try:
            client.upsert(
                collection_name=settings.qdrant_collection,
                points=points,
                wait=True,  # 写完才返回，保证一致性；写入频率低可接受
            )
        except Exception:
            logger.warning("Qdrant upsert 失败，本批写入跳过", exc_info=True)

    def delete(self, fingerprints: list[str]) -> None:
        """FIX: R7-T4 —— 按指纹删除 point（确定性 uuid5 point id 反推）。

        KB LRU 驱逐 / clear() 同步调用，避免被淘汰条目的向量点永久残留、
        _try_vector_rag 继续召回已淘汰的历史结论。失败静默降级。
        """
        if not fingerprints:
            return
        client = _get_qdrant_client()
        if client is None:
            return
        try:
            from qdrant_client.models import PointIdsList
        except ImportError:
            logger.warning("qdrant-client 未安装，delete 跳过")
            return

        point_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_DNS, str(fp)))
            for fp in fingerprints
            if fp
        ]
        if not point_ids:
            return
        try:
            client.delete(
                collection_name=settings.qdrant_collection,
                points_selector=PointIdsList(points=point_ids),
                wait=True,
            )
        except Exception:
            logger.warning("Qdrant delete 失败，忽略", exc_info=True)

    def search(self, query: str, top_k: int) -> list[tuple[dict[str, Any], float]]:
        if not query or top_k <= 0:
            return []
        client = _get_qdrant_client()
        if client is None:
            return []
        if _get_embedding_client() is None:
            return []

        vectors = _embed_texts([query])
        if vectors is None:
            return []

        try:
            hits = client.search(
                collection_name=settings.qdrant_collection,
                query_vector=vectors[0],
                limit=top_k,
                score_threshold=settings.vector_store_min_score,
            )
            return [(hit.payload, float(hit.score)) for hit in hits]
        except Exception:
            logger.warning("Qdrant search 失败，返回空结果", exc_info=True)
            return []


def _reset_qdrant_state() -> None:
    """测试辅助：重置模块级单例（仅供单测使用，生产代码不应调用）。"""
    global _qdrant_client, _qdrant_collection_ready, _embedding_client
    with _qdrant_lock:
        _qdrant_client = None
        _qdrant_collection_ready = False
    with _embedding_client_lock:
        _embedding_client = None

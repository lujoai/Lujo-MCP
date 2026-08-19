"""LLM 分析结果多级缓存（P1-2 / P3-7）—— L1 LRU + L2 Redis。

从 analyzer.py 拆出（god object 重构）：
- L1：进程内 OrderedDict LRU（容量 100）
- L2：Redis（TTL 3600s），不可用时静默降级为仅 L1
- 指纹以 error-surface 为主键（P1-8），不含 request_id 噪声
"""

import copy
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional

from app.config import settings
from app.llm.context_prep import _get_error_signal

logger = logging.getLogger("lujo-mcp.llm")

# ── L2 Redis 缓存客户端（Phase 3.3）──
# 惰性初始化；Redis 不可用时为 None，降级为仅 L1 内存缓存
_redis_cache_client: Optional[object] = None
_redis_cache_initialized: bool = False
_redis_cache_lock = threading.Lock()

# ── LLM 分析结果缓存（P1-2）──
# 按 fingerprint 缓存 LLM 分析结果，避免相同上下文重复调用
# 使用 OrderedDict 实现真正的 LRU：
#   - 命中时 move_to_end(key)，把最近访问的条目移到链表末尾
#   - 容量超限时 popitem(last=False)，淘汰链表头部（最久未访问）的条目
_MAX_CACHE_SIZE = 100
_CACHE_TTL_SECONDS = 3600
_analysis_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()


def _compute_context_fingerprint(context: dict) -> str:
    """计算上下文指纹，用于缓存命中判定。

    FIX: P1-8 以 error-surface 为主键（异常 fingerprint/type/message + key frames
    file:line:function），不再包含 request_id —— 否则每次请求唯一、
    缓存命中率趋近 0。缓存淘汰与 TTL 语义不变（L1 LRU + TTL，L2 Redis）。
    """
    exc_type, message, fingerprint = _get_error_signal(context)

    # key frames：取异常的前几帧（file:line:function），忽略请求维度噪声
    frames: list = []
    exception = context.get("exception")
    if isinstance(exception, dict):
        frames = exception.get("frames") or []
    if not frames:
        for error in context.get("errors", []) or []:
            if isinstance(error, dict) and error.get("frames"):
                frames = error["frames"]
                break
    key_frames: list[str] = []
    for f in (frames or [])[:3]:
        if isinstance(f, dict):
            key_frames.append(
                f"{f.get('file', '')}:{f.get('line', '')}:{f.get('function', '')}"
            )
        else:
            key_frames.append(str(f))

    # error-surface：无 exception 字段时，把 errors 条目的 type/message/fingerprint
    # 纳入指纹（结构化而非整串序列化），保持"不同错误不同指纹"的同时去除 request_id 噪声
    error_surface: list[str] = []
    for error in context.get("errors", []) or []:
        if isinstance(error, dict):
            error_surface.append(
                f"{error.get('type') or error.get('exception_type') or ''}"
                f":{error.get('message') or error.get('msg') or ''}"
                f":{error.get('fingerprint') or ''}"
            )
        else:
            error_surface.append(str(error))

    key_parts = [
        fingerprint or "",
        exc_type,
        message,
        "|".join(key_frames),
        "|".join(error_surface),
    ]
    return hashlib.sha256("|".join(key_parts).encode()).hexdigest()[:16]


def _get_cached_result(fingerprint: str) -> Optional[dict]:
    """获取缓存结果（多级缓存 L1+L2），未命中或过期则返回 None。

    查找顺序：L1(OrderedDict LRU) → L2(Redis)。
    L2 命中时回填 L1。Redis 不可用时静默降级为仅 L1。
    返回深拷贝以保护缓存不可变性。
    """
    # ── L1: OrderedDict LRU ──
    with _cache_lock:
        entry = _analysis_cache.get(fingerprint)
        if entry:
            if time.time() - entry["cached_at"] > _CACHE_TTL_SECONDS:
                del _analysis_cache[fingerprint]
                entry = None
            else:
                # LRU：命中后移到末尾，保持"末尾=最近访问"顺序
                _analysis_cache.move_to_end(fingerprint)
                return copy.deepcopy(entry["result"])

    # ── L2: Redis ──
    redis_client = _get_redis_cache()
    if redis_client is not None:
        try:
            raw = redis_client.get(f"ai-debug:llm:cache:{fingerprint}")
            if raw:
                result = json.loads(raw)
                # L2 命中 → 回填 L1
                _set_cache_result(fingerprint, result)
                logger.info("LLM cache L2 hit (fingerprint=%s)", fingerprint)
                return copy.deepcopy(result)
        except Exception:
            logger.warning("L2 Redis 缓存读取失败，降级为 L1", exc_info=True)

    return None


def _set_cache_result(fingerprint: str, result: dict) -> None:
    """设置缓存结果（多级缓存 L1+L2）。

    写 L1(OrderedDict LRU，超容量淘汰最久未使用) + L2(Redis, TTL=3600s)。
    Redis 不可用时静默降级为仅 L1。
    """
    # ── L1: OrderedDict LRU ──
    with _cache_lock:
        is_new = fingerprint not in _analysis_cache
        if is_new and len(_analysis_cache) >= _MAX_CACHE_SIZE:
            # 容量已满且为新键：淘汰链表头部最久未访问的条目
            _analysis_cache.popitem(last=False)
        _analysis_cache[fingerprint] = {
            "result": result,
            "cached_at": time.time(),
        }
        if not is_new:
            # 已存在键：赋值不改变位置，显式移到末尾标记最近使用
            _analysis_cache.move_to_end(fingerprint)

    # ── L2: Redis ──
    redis_client = _get_redis_cache()
    if redis_client is not None:
        try:
            redis_client.setex(
                f"ai-debug:llm:cache:{fingerprint}",
                _CACHE_TTL_SECONDS,
                json.dumps(result, ensure_ascii=False, default=str),
            )
        except Exception:
            logger.warning("L2 Redis 缓存写入失败，降级为 L1", exc_info=True)


def _set_l1_only(fingerprint: str, result: dict) -> None:
    """仅写入 L1 缓存（OrderedDict LRU），不写 L2、不刷新 L2 TTL。

    仅供 ``app.llm.cache_prewarm`` 使用——预热场景下 L2 已有数据，
    若调 ``_set_cache_result`` 会 ``setex`` 刷新 L2 TTL，导致定时预热
    周期下 L2 永不自然淘汰，违反 TTL 淘汰语义。本函数让 L2 TTL 自然流逝，
    该过期的过期，下次 SCAN 时自然不在结果集里。

    LRU 逻辑与 ``_set_cache_result`` 的 L1 段完全一致：容量满且新键时
    ``popitem(last=False)`` 淘汰最久未访问；已存在键 ``move_to_end``。
    必须与 ``_set_cache_result`` 共享 ``_cache_lock`` 避免并发竞态。
    """
    with _cache_lock:
        is_new = fingerprint not in _analysis_cache
        if is_new and len(_analysis_cache) >= _MAX_CACHE_SIZE:
            _analysis_cache.popitem(last=False)
        _analysis_cache[fingerprint] = {
            "result": result,
            "cached_at": time.time(),
        }
        if not is_new:
            _analysis_cache.move_to_end(fingerprint)


def _get_redis_cache():
    """惰性获取 Redis 客户端（L2 缓存），不可用时返回 None。

    Redis 不可用时静默降级为仅 L1 内存缓存，不影响功能。
    采用双重检查 + threading.Lock 保证线程安全。
    """
    global _redis_cache_client, _redis_cache_initialized
    if _redis_cache_initialized:
        return _redis_cache_client
    with _redis_cache_lock:
        if not _redis_cache_initialized:
            try:
                import redis as _redis_module
                client = _redis_module.Redis.from_url(
                    settings.redis_url,
                    socket_timeout=2,
                    decode_responses=True,
                )
                client.ping()  # 测试连接可用性
                _redis_cache_client = client
                logger.info("LLM L2 Redis 缓存已连接")
            except Exception:
                logger.warning("Redis L2 缓存不可用，降级为仅 L1 内存缓存")
                _redis_cache_client = None
            finally:
                _redis_cache_initialized = True
    return _redis_cache_client

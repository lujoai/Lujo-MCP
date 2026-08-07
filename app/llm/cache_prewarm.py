"""P3-7 L3 缓存预热 —— 服务启动时（+可选定时）从 L2 Redis 回填 L1

设计要点（成交条件）：

- **数据源**：L2 Redis ``SCAN ai-debug:llm:cache:*``。L2 跨重启持久化；KB 是进程内
  OrderedDict 不持久，不能作为预热源；PG errors 表 fingerprint 与 LLM 缓存 fingerprint
  非同域，无法直接关联。
- **只写 L1 不写 L2**：通过 ``app.llm.analyzer._set_l1_only`` 写入，**不**调
  ``_set_cache_result``。若调 ``_set_cache_result`` 会 ``setex`` 刷新 L2 TTL，导致定时
  预热周期下 L2 永不自然淘汰，违反 TTL 淘汰语义。本模块让 L2 TTL 自然流逝，该过期的
  过期，下次 SCAN 时自然不在结果集里。
- **v1 用 SCAN 顺序取 top_n**（非访问频率 top N）。对冷启动目标（避免全量 miss）
  足够——任何 N 条缓存都比 0 条好。频率 top N 留待 v2（需在缓存区 ``ZINCRBY`` 侵入）。
- **fail-safe**：L2 不可用 / SCAN 失败 / 反序列化失败 → 记 warning 返回 stats，不抛
  异常、不阻塞服务启动。
- **隔离性**：analyzer.py 缓存区零改动（``_get_cached_result`` / ``_set_cache_result``
  / ``analyze_async`` 一行不动），仅通过 ``_set_l1_only`` 公共 API 写入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Optional

from app.config import settings
from app.llm.analyzer import (
    _MAX_CACHE_SIZE,
    _get_redis_cache,
    _set_l1_only,
)

logger = logging.getLogger("ai-debug-mcp.cache-prewarm")

# L2 Redis key 命名空间（与 analyzer.py:_set_cache_result 中的 setex key 一致）
_L2_KEY_PREFIX = "ai-debug:llm:cache:"

# 定时任务的模块级单例
_prewarm_task: Optional[asyncio.Task] = None


def _empty_stats() -> dict:
    """返回零值 stats 模板。"""
    return {"scanned": 0, "prewarmed": 0, "skipped": 0}


def prewarm_cache(top_n: int) -> dict:
    """从 L2 Redis 扫描 top_n 个缓存 entry，回填 L1。

    同步函数（Redis 客户端是同步的）；lifespan 中通过 ``asyncio.to_thread`` 或
    ``prewarm_once_with_timeout`` 包装调用。

    Args:
        top_n: 预热条数上限；超过 ``_MAX_CACHE_SIZE`` 会被 cap + warning

    Returns:
        ``{"scanned": int, "prewarmed": int, "skipped": int}``；
        ``scanned`` = SCAN 取到的 key 数；
        ``prewarmed`` = 成功写入 L1 的条数；
        ``skipped`` = MGET 返回 None 或反序列化失败的条数。
    """
    stats = _empty_stats()

    # 容量保护：cap 到 L1 _MAX_CACHE_SIZE，避免 prewarm 自我淘汰
    if top_n > _MAX_CACHE_SIZE:
        logger.warning(
            "prewarm top_n=%d exceeds L1 max_size=%d, capping",
            top_n,
            _MAX_CACHE_SIZE,
        )
        top_n = _MAX_CACHE_SIZE
    if top_n <= 0:
        return stats

    redis_client = _get_redis_cache()
    if redis_client is None:
        # L2 不可用时静默降级，不算错误
        logger.info("prewarm skipped: L2 Redis unavailable")
        return stats

    try:
        # SCAN 累计 top_n 个 key 即 break（顺序非频率，v1 设计取舍）
        keys: list[str] = []
        scan_count = max(top_n * 2, 100)
        for key in redis_client.scan_iter(
            match=f"{_L2_KEY_PREFIX}*", count=scan_count
        ):
            keys.append(key)
            if len(keys) >= top_n:
                break
        stats["scanned"] = len(keys)
        if not keys:
            logger.info("prewarm: L2 empty, nothing to prewarm")
            return stats

        # MGET 批量读，按 100 一块分块（top_n 已 cap 到 100，通常一次 MGET 即可）
        results: list[tuple[str, Optional[str]]] = []
        for i in range(0, len(keys), 100):
            chunk = keys[i : i + 100]
            values = redis_client.mget(*chunk)
            results.extend(zip(chunk, values))

        for key, raw in results:
            fingerprint = key.removeprefix(_L2_KEY_PREFIX)
            if raw is None:
                # MGET 前该 key 已过期 → 跳过
                stats["skipped"] += 1
                continue
            try:
                result = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                # 单条损坏不中断整批
                stats["skipped"] += 1
                logger.debug(
                    "prewarm skip bad json: key=%s", key, exc_info=True
                )
                continue
            try:
                _set_l1_only(fingerprint, result)
                stats["prewarmed"] += 1
            except Exception:
                stats["skipped"] += 1
                logger.debug(
                    "prewarm skip L1 write failure: fp=%s",
                    fingerprint,
                    exc_info=True,
                )
    except Exception:
        # fail-safe：任何未预期异常记 warning，返回当前 stats
        logger.warning(
            "prewarm failed, returning partial stats: %s", stats, exc_info=True
        )

    logger.info("prewarm done: %s", stats)
    return stats


async def prewarm_once_with_timeout(
    top_n: int, timeout: float = 10.0
) -> dict:
    """供 lifespan startup 调用的包装：限时执行 prewarm_cache，超时不抛。

    Args:
        top_n: 预热条数上限
        timeout: 超时秒数（默认 10s）；超时返回 ``{..., "timeout": True}``

    Returns:
        prewarm_cache 的 stats；超时时追加 ``"timeout": True``。
    """
    try:
        stats = await asyncio.wait_for(
            asyncio.to_thread(prewarm_cache, top_n),
            timeout=timeout,
        )
        return stats
    except asyncio.TimeoutError:
        logger.warning(
            "prewarm timed out after %.1fs, skipping startup prewarm", timeout
        )
        stats = _empty_stats()
        stats["timeout"] = True
        return stats
    except Exception:
        # 兜底：任何未预期异常都不应阻塞服务启动
        logger.warning(
            "prewarm_once_with_timeout unexpected error", exc_info=True
        )
        stats = _empty_stats()
        stats["timeout"] = True
        return stats


async def _prewarm_loop() -> None:
    """定时预热循环：首次 jitter 错峰，之后按 interval 周期执行。

    单次 prewarm 失败不退出循环（fail-soft）；收到 CancelledError 时优雅退出。
    """
    interval = settings.llm_cache_prewarm_interval_seconds
    top_n = settings.llm_cache_prewarm_top_n

    # 首次 jitter 错峰，缓解多 worker thundering herd
    await asyncio.sleep(random.uniform(0, interval))
    while True:
        try:
            await asyncio.to_thread(prewarm_cache, top_n)
        except asyncio.CancelledError:
            # 优雅退出
            return
        except Exception:
            # 单次失败不退出循环
            logger.warning(
                "periodic prewarm failed, will retry next interval",
                exc_info=True,
            )
        await asyncio.sleep(interval)


def start_prewarm_task() -> None:
    """启动定时预热任务。

    - 若已有任务在跑，先停止旧任务（避免泄漏）
    - ``llm_cache_prewarm_interval_seconds == 0`` 时不创建任务（仅 startup 一次性预热）
    - 任务内含 jitter 错峰与 fail-soft 重试
    """
    global _prewarm_task

    if _prewarm_task is not None:
        # 已有任务在跑：直接 cancel，避免泄漏（start_prewarm_task 是同步函数，
        # 无法 await stop_prewarm_task，用 cancel() 替代）
        _prewarm_task.cancel()
        _prewarm_task = None

    if settings.llm_cache_prewarm_interval_seconds <= 0:
        # interval=0：仅 startup 一次性预热，不创建定时任务
        logger.info(
            "prewarm task not started: interval=0 (startup-only prewarm mode)"
        )
        return

    _prewarm_task = asyncio.create_task(_prewarm_loop())
    logger.info(
        "prewarm task started: interval=%ds top_n=%d",
        settings.llm_cache_prewarm_interval_seconds,
        settings.llm_cache_prewarm_top_n,
    )


async def stop_prewarm_task() -> None:
    """停止定时预热任务（幂等）。

    采用 ``cancel() + await`` + 抑制 ``CancelledError`` 的严谨取消语义，
    对齐 ``app.llm.analysis_queue.drain`` 的写法，避免 orphaned task。
    ``_prewarm_task is None`` 时幂等返回。
    """
    global _prewarm_task

    if _prewarm_task is None:
        return

    _prewarm_task.cancel()
    try:
        await _prewarm_task
    except asyncio.CancelledError:
        pass
    _prewarm_task = None
    logger.info("prewarm task stopped")

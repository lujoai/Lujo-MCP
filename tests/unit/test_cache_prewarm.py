"""单元测试：P3-7 L3 缓存预热。

覆盖目标：
- prewarm_cache 从 L2 SCAN → MGET → 写 L1 的主链路
- fail-safe：L2 不可用 / SCAN 失败 / 反序列化失败 不抛异常
- 只写 L1 不写 L2（关键回归，断言 setex 调用次数为 0）
- top_n 容量 cap（不超过 L1 _MAX_CACHE_SIZE）
- 定时任务的启停与周期循环
- prewarm_once_with_timeout 的超时保护
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from app.llm import analyzer as analyzer_module
from app.llm import cache_prewarm as prewarm_module

# 注意：prewarm_module 在模块加载时通过 `from app.llm.analyzer import _get_redis_cache`
# 把 _get_redis_cache 绑定到自己的命名空间。因此 patch 必须落在 prewarm_module 上，
# 而不是 analyzer_module——否则 prewarm_module 内的引用不会被替换。


# ── autouse fixture：每个测试前重置 L1 + L2 状态 ──
# 对齐 tests/integration/test_redis_cache_integration.py:48-59 的风格
@pytest.fixture(autouse=True)
def reset_cache_state():
    analyzer_module._analysis_cache.clear()
    analyzer_module._redis_cache_client = None
    analyzer_module._redis_cache_initialized = False
    # 重置 prewarm 任务单例
    if prewarm_module._prewarm_task is not None:
        prewarm_module._prewarm_task = None
    yield
    # 清理：若有遗留 task 则取消（避免测试间泄漏）
    if prewarm_module._prewarm_task is not None:
        prewarm_module._prewarm_task.cancel()
        prewarm_module._prewarm_task = None
    analyzer_module._analysis_cache.clear()
    analyzer_module._redis_cache_client = None
    analyzer_module._redis_cache_initialized = False


def _make_mock_redis(scan_keys: list[str], mget_values: list):
    """构造 mock Redis 客户端：scan_iter 返回 scan_keys，mget 返回 mget_values。"""
    client = MagicMock()
    client.scan_iter.return_value = iter(scan_keys)
    client.mget.return_value = mget_values
    return client


# ────────────────────────────────────────────────────────────
# 1. 主链路：从 L2 加载 top_n 到 L1
# ────────────────────────────────────────────────────────────
def test_prewarm_from_l2_loads_top_n_into_l1():
    """SCAN 返回 5 个 key + MGET 返回 5 个 JSON → L1 有 5 条、prewarmed=5。"""
    keys = [f"ai-debug:llm:cache:fp00{i}" for i in range(5)]
    payloads = [{"root_cause": f"cause{i}"} for i in range(5)]
    mget_values = [json.dumps(p, ensure_ascii=False) for p in payloads]
    mock_redis = _make_mock_redis(keys, mget_values)

    with patch.object(prewarm_module, "_get_redis_cache", return_value=mock_redis):
        stats = prewarm_module.prewarm_cache(top_n=20)

    assert stats["scanned"] == 5
    assert stats["prewarmed"] == 5
    assert stats["skipped"] == 0
    # L1 写入 5 条
    assert len(analyzer_module._analysis_cache) == 5
    # fingerprint 去前缀后作为 L1 key
    for i in range(5):
        assert f"fp00{i}" in analyzer_module._analysis_cache


# ────────────────────────────────────────────────────────────
# 2. fail-safe：L2 不可用
# ────────────────────────────────────────────────────────────
def test_prewarm_skips_when_l2_unavailable():
    """_get_redis_cache 返回 None → stats 全 0、不抛。"""
    with patch.object(prewarm_module, "_get_redis_cache", return_value=None):
        stats = prewarm_module.prewarm_cache(top_n=20)

    assert stats == {"scanned": 0, "prewarmed": 0, "skipped": 0}
    assert len(analyzer_module._analysis_cache) == 0


# ────────────────────────────────────────────────────────────
# 3. fail-safe：SCAN 异常
# ────────────────────────────────────────────────────────────
def test_prewarm_handles_scan_errors():
    """scan_iter raise RedisError → warning + stats 全 0、不抛。"""
    mock_redis = MagicMock()
    mock_redis.scan_iter.side_effect = Exception("redis connection lost")

    with patch.object(prewarm_module, "_get_redis_cache", return_value=mock_redis):
        stats = prewarm_module.prewarm_cache(top_n=20)

    # fail-safe：异常被捕获，stats 为 _empty_stats()
    assert stats["scanned"] == 0
    assert stats["prewarmed"] == 0
    assert len(analyzer_module._analysis_cache) == 0


# ────────────────────────────────────────────────────────────
# 4. 反序列化失败：单条损坏不中断整批
# ────────────────────────────────────────────────────────────
def test_prewarm_handles_deserialize_errors():
    """MGET 返回 ['valid_json', 'not-json', None] → 1 prewarmed / 2 skipped。"""
    keys = [
        "ai-debug:llm:cache:good",
        "ai-debug:llm:cache:bad",
        "ai-debug:llm:cache:missing",
    ]
    mget_values = [
        json.dumps({"root_cause": "ok"}),
        "not-json",
        None,  # MGET 前该 key 已过期
    ]
    mock_redis = _make_mock_redis(keys, mget_values)

    with patch.object(prewarm_module, "_get_redis_cache", return_value=mock_redis):
        stats = prewarm_module.prewarm_cache(top_n=20)

    assert stats["scanned"] == 3
    assert stats["prewarmed"] == 1
    assert stats["skipped"] == 2
    assert "good" in analyzer_module._analysis_cache
    assert "bad" not in analyzer_module._analysis_cache
    assert "missing" not in analyzer_module._analysis_cache


# ────────────────────────────────────────────────────────────
# 5. top_n 截断
# ────────────────────────────────────────────────────────────
def test_prewarm_respects_top_n_limit():
    """SCAN 返回 50 个 key、top_n=20 → 仅取前 20 个、prewarmed≤20。"""
    keys = [f"ai-debug:llm:cache:fp{i:03d}" for i in range(50)]
    mget_values = [json.dumps({"i": i}) for i in range(50)]
    mock_redis = _make_mock_redis(keys, mget_values)

    with patch.object(prewarm_module, "_get_redis_cache", return_value=mock_redis):
        stats = prewarm_module.prewarm_cache(top_n=20)

    assert stats["scanned"] == 20  # SCAN 在累计到 top_n 时 break
    assert stats["prewarmed"] == 20
    assert len(analyzer_module._analysis_cache) == 20


# ────────────────────────────────────────────────────────────
# 6. 定时任务启停
# ────────────────────────────────────────────────────────────
def test_prewarm_task_start_stop():
    """start 后 _prewarm_task 非 None、stop 后为 None。"""
    # 用 monkeypatch 把 interval 设为非 0（避免 start 直接 return）
    with patch.object(
        prewarm_module.settings, "llm_cache_prewarm_interval_seconds", 3600
    ), patch.object(
        prewarm_module.settings, "llm_cache_prewarm_top_n", 5
    ):
        async def _run():
            prewarm_module.start_prewarm_task()
            assert prewarm_module._prewarm_task is not None
            await prewarm_module.stop_prewarm_task()
            assert prewarm_module._prewarm_task is None

        asyncio.run(_run())


# ────────────────────────────────────────────────────────────
# 7. 定时任务周期循环
# ────────────────────────────────────────────────────────────
def test_prewarm_task_periodic_interval():
    """interval=极短 + mock prewarm_cache 为同步函数，验证被调用 ≥2 次。

    注意：prewarm_cache 是同步函数（被 asyncio.to_thread 包装），
    mock 时也必须用同步函数，否则 asyncio.to_thread 会返回 coroutine 而非结果。
    """
    call_count = 0

    def _fake_prewarm(top_n):
        nonlocal call_count
        call_count += 1
        return {"scanned": 0, "prewarmed": 0, "skipped": 0}

    async def _run():
        # 用极短 interval 让循环快速跑多次；jitter 也置 0
        with patch.object(
            prewarm_module.settings, "llm_cache_prewarm_interval_seconds", 0.01
        ), patch.object(
            prewarm_module.settings, "llm_cache_prewarm_top_n", 5
        ), patch.object(
            prewarm_module, "prewarm_cache", side_effect=_fake_prewarm
        ), patch.object(
            prewarm_module.random, "uniform", return_value=0
        ):
            prewarm_module.start_prewarm_task()
            # 让事件循环跑一会儿，让定时任务有机会执行多次
            await asyncio.sleep(0.1)
            await prewarm_module.stop_prewarm_task()

    asyncio.run(_run())
    # 至少被调用 2 次（首次错峰后 + 至少一次周期触发）
    assert call_count >= 2


# ────────────────────────────────────────────────────────────
# 8. fingerprint 去前缀
# ────────────────────────────────────────────────────────────
def test_prewarm_strips_key_prefix_correctly():
    """SCAN 返回 'ai-debug:llm:cache:fp001'，断言 L1 key 是 'fp001'。"""
    keys = ["ai-debug:llm:cache:fp001"]
    mget_values = [json.dumps({"root_cause": "ok"})]
    mock_redis = _make_mock_redis(keys, mget_values)

    with patch.object(prewarm_module, "_get_redis_cache", return_value=mock_redis):
        prewarm_module.prewarm_cache(top_n=20)

    assert "fp001" in analyzer_module._analysis_cache
    # 完整 key 不应作为 L1 key（避免前缀污染）
    assert "ai-debug:llm:cache:fp001" not in analyzer_module._analysis_cache


# ────────────────────────────────────────────────────────────
# 9. 关键回归：prewarm 不触碰 L2 TTL（不调 setex）
# ────────────────────────────────────────────────────────────
def test_prewarm_does_not_touch_l2_ttl():
    """prewarm 后断言 Redis 客户端的 setex 调用次数为 0。

    这是设计决策的关键回归测试：若误用 _set_cache_result 会刷新 L2 TTL，
    导致定时预热周期下 L2 永不淘汰。
    """
    keys = [f"ai-debug:llm:cache:fp{i}" for i in range(3)]
    mget_values = [json.dumps({"i": i}) for i in range(3)]
    mock_redis = _make_mock_redis(keys, mget_values)

    with patch.object(prewarm_module, "_get_redis_cache", return_value=mock_redis):
        prewarm_module.prewarm_cache(top_n=20)

    # 关键断言：setex 一次都没调用
    mock_redis.setex.assert_not_called()
    # 但 mget 被调用了（确认 mock 正常工作）
    mock_redis.mget.assert_called_once()


# ────────────────────────────────────────────────────────────
# 10. top_n cap 到 _MAX_CACHE_SIZE
# ────────────────────────────────────────────────────────────
def test_prewarm_caps_top_n_to_l1_max_size():
    """top_n=200，断言实际 SCAN 取的 key 数 ≤ _MAX_CACHE_SIZE=100。"""
    # SCAN 提供足够多的 key（200 个），看 prewarm 是否真的 cap 到 100
    keys = [f"ai-debug:llm:cache:fp{i:03d}" for i in range(200)]
    mget_values = [json.dumps({"i": i}) for i in range(200)]
    mock_redis = _make_mock_redis(keys, mget_values)

    with patch.object(prewarm_module, "_get_redis_cache", return_value=mock_redis):
        stats = prewarm_module.prewarm_cache(top_n=200)

    # cap 后 top_n=100，SCAN 累计到 100 即 break
    assert stats["scanned"] <= analyzer_module._MAX_CACHE_SIZE
    assert stats["scanned"] == 100
    assert stats["prewarmed"] == 100


# ────────────────────────────────────────────────────────────
# 11. prewarm_once_with_timeout 超时保护
# ────────────────────────────────────────────────────────────
def test_prewarm_once_with_timeout():
    """mock prewarm_cache 为同步阻塞函数，超时后断言返回 timeout=True 且不抛。

    注意：prewarm_once_with_timeout 用 asyncio.to_thread(prewarm_cache, ...) 包装
    同步函数。mock 必须是同步的（time.sleep 阻塞线程），由 to_thread 放入线程池，
    wait_for 在 timeout 后取消 wait（线程仍在后台跑但不影响测试结果）。
    """
    def _slow_prewarm(top_n):
        # 模拟 Redis 慢查询，阻塞线程超过 timeout
        time.sleep(2)
        return {"scanned": 0, "prewarmed": 0, "skipped": 0}

    async def _run():
        with patch.object(prewarm_module, "prewarm_cache", side_effect=_slow_prewarm):
            stats = await prewarm_module.prewarm_once_with_timeout(
                top_n=20, timeout=0.1
            )
        return stats

    stats = asyncio.run(_run())
    assert stats.get("timeout") is True
    assert stats["scanned"] == 0
    assert stats["prewarmed"] == 0


# ────────────────────────────────────────────────────────────
# 附加：prewarm_once_with_timeout 正常路径
# ────────────────────────────────────────────────────────────
def test_prewarm_once_with_timeout_normal_path():
    """正常完成时返回 prewarm_cache 的 stats，无 timeout 字段。"""

    async def _run():
        keys = ["ai-debug:llm:cache:fp_ok"]
        mget_values = [json.dumps({"root_cause": "ok"})]
        mock_redis = _make_mock_redis(keys, mget_values)
        with patch.object(
            prewarm_module, "_get_redis_cache", return_value=mock_redis
        ):
            stats = await prewarm_module.prewarm_once_with_timeout(
                top_n=20, timeout=5.0
            )
        return stats

    stats = asyncio.run(_run())
    assert stats["scanned"] == 1
    assert stats["prewarmed"] == 1
    assert "timeout" not in stats

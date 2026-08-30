"""共享状态后端单测 —— 重点覆盖 P1-10c 限流窗口 key 驱逐。

历史背景：_evict_if_needed 只按 _data 长度驱逐，而 allow() 滑动窗口时间戳
key 只写入 _timestamps、从不进入 _data → 高基数限流键无限增长。修复后两表
任一超限即合并驱逐；incr_float 也触发驱逐。
"""

from app.state.store import MemoryStateStore


def _store_with_small_limit(limit: int = 10) -> MemoryStateStore:
    store = MemoryStateStore()
    store._MAX_ENTRIES = limit
    return store

def test_allow_high_cardinality_keys_are_evicted():
    """allow() 只写 _timestamps 的限流 key 也必须受上限约束并被驱逐。"""
    store = _store_with_small_limit(limit=10)
    for i in range(30):
        store.allow(f"rl:{i}", limit=100, window=60)

    assert len(store._timestamps) <= 10


def test_incr_float_triggers_eviction():
    """incr_float 高频 key 触发驱逐（此前纯 incr_float 可绕过上限）。"""
    store = _store_with_small_limit(limit=10)
    for i in range(30):
        store.incr_float(f"metric:{i}", 1.0)

    assert len(store._data) <= 10


def test_eviction_cleans_both_tables():
    """驱逐同时清理 _data 与 _timestamps，两表均保持有界。"""
    store = _store_with_small_limit(limit=10)
    for i in range(30):
        store.allow(f"shared:{i}", limit=100, window=60)
        store.incr(f"shared:{i}", 1)

    assert len(store._timestamps) <= 10
    assert len(store._data) <= 10


def test_eviction_keeps_active_entries_usable():
    """驱逐后剩余 key 仍正常可读，不影响功能。"""
    store = _store_with_small_limit(limit=10)
    for i in range(20):
        store.allow(f"rl:{i}", limit=100, window=60)
        store.incr_float(f"m:{i}", 1.5)

    # 存活 key 的计数语义不受驱逐影响
    for i in range(20):
        val = store.get(f"m:{i}")
        assert val in (0.0, 1.5)


def test_redis_store_methods_fail_closed():
    """Redis 后端全部方法 fail-closed（FIX b6-2）：Redis 异常不穿透调用方。

    allow() 原本就捕获异常返回 False，incr/incr_float/get/keys 此前裸抛；
    现统一返回安全默认值（0 / 0.0 / []），与 allow() 口径一致。
    """
    from app.state.store import RedisStateStore

    class _BoomRedis:
        def incr(self, *a, **k):
            raise RuntimeError("redis down")

        def incrbyfloat(self, *a, **k):
            raise RuntimeError("redis down")

        def get(self, *a, **k):
            raise RuntimeError("redis down")

        def scan_iter(self, *a, **k):
            raise RuntimeError("redis down")

    store = RedisStateStore.__new__(RedisStateStore)
    store._r = _BoomRedis()

    assert store.incr("k") == 0
    assert store.incr_float("k", 1.0) == 0.0
    assert store.get("k") == 0.0
    assert store.keys("p") == []

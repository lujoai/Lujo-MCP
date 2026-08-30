"""共享状态后端 —— 限流计数与指标计数可插拔（内存 / Redis）

生产多实例部署时必须使用 Redis，否则每个 worker 各自计数，
限流形同虚设、指标只反映单实例。
"""

import logging
import time
import threading
import uuid
from abc import ABC, abstractmethod
from typing import List, Optional

from app.config import settings

logger = logging.getLogger("lujo-mcp.state")


class StateStore(ABC):
    """共享状态抽象接口"""

    @abstractmethod
    def allow(self, key: str, limit: int, window: int) -> bool:
        """滑动窗口限流：window 秒内最多 limit 次，返回是否放行"""

    @abstractmethod
    def incr(self, key: str, by: int = 1) -> int:
        """整数计数器自增，返回当前值"""

    @abstractmethod
    def incr_float(self, key: str, by: float) -> float:
        """浮点计数器自增，返回当前值"""

    @abstractmethod
    def get(self, key: str) -> float:
        """读取计数（整数或浮点）"""

    @abstractmethod
    def keys(self, prefix: str) -> List[str]:
        """列出指定前缀的所有键"""

    def close(self) -> None:
        pass


class MemoryStateStore(StateStore):
    """内存实现（开发 / 单机默认）"""

    _MAX_ENTRIES = 100_000

    def __init__(self):
        self._data: dict[str, float] = {}
        self._timestamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: int) -> bool:
        now = time.time()
        with self._lock:
            ts = self._timestamps.setdefault(key, [])
            cutoff = now - window
            self._timestamps[key] = [t for t in ts if t > cutoff]
            if len(self._timestamps[key]) >= limit:
                return False
            self._timestamps[key].append(now)
            self._evict_if_needed()
        return True

    def incr(self, key: str, by: int = 1) -> int:
        with self._lock:
            self._data[key] = self._data.get(key, 0) + by
            self._evict_if_needed()
            # FIX: P1-10c 驱逐可能命中刚写入的 key（有界性权衡：高基数下丢最旧），
            # 返回时用 .get 防御避免 KeyError
            return int(self._data.get(key, 0))

    def incr_float(self, key: str, by: float) -> float:
        with self._lock:
            self._data[key] = self._data.get(key, 0.0) + by
            # FIX: P1-10c incr_float 也触发驱逐（此前只有 allow/incr 触发，
            # 纯 incr_float 高频 key 可绕过上限）
            self._evict_if_needed()
            return float(self._data.get(key, 0.0))

    def get(self, key: str) -> float:
        with self._lock:
            return self._data.get(key, 0)

    def keys(self, prefix: str) -> List[str]:
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]

    def _evict_if_needed(self) -> None:
        """当 _data 或 _timestamps 超过上限时，按插入序（最旧 key）驱逐一半。

        FIX: P1-10c allow() 滑动窗口时间戳 key 只写入 _timestamps、从不进入
        _data，此前只按 _data 长度驱逐 → 高基数限流键无限增长。现在两表
        任一超限即合并驱逐，保证限流 key 同样受上限约束。
        """
        if len(self._data) < self._MAX_ENTRIES and len(self._timestamps) < self._MAX_ENTRIES:
            return
        keys = list(dict.fromkeys(list(self._data.keys()) + list(self._timestamps.keys())))
        drop = len(keys) // 2
        for k in keys[:drop]:
            self._data.pop(k, None)
            self._timestamps.pop(k, None)


class RedisStateStore(StateStore):
    """Redis 实现（多实例共享）

    FIX(v0.7.1-b6-2): 统一 fail-closed——此前 allow() 捕获 Redis 异常返回 False，
    但 incr/incr_float/get/keys 裸抛，Redis 短暂不可达会穿透异常到调用方。
    现全部方法捕获异常并返回安全默认值（0 / 0.0 / []），与 allow() 一致。
    """

    # Lua script for atomic sliding-window rate limiting using ZSET.
    # 与 MemoryStateStore.allow 语义一致：滑动窗口内记录每个请求时间戳，
    # 超过 limit 则拒绝（不记录本次时间戳），未超则记录并放行。
    _SLIDING_WINDOW_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

-- 清除窗口外的过期时间戳
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)

-- 统计窗口内当前请求数
local count = redis.call('ZCARD', KEYS[1])

if count >= limit then
    return 0
end

-- 记录本次请求时间戳；ARGV[4] 为唯一 member，避免同时间戳 ZADD 合并
redis.call('ZADD', KEYS[1], now, ARGV[4])

-- 设置 key 过期时间，防止空闲后残留
redis.call('EXPIRE', KEYS[1], window)

return 1
"""

    def __init__(self, url: str):
        import redis
        self._r = redis.Redis.from_url(url, socket_timeout=2, decode_responses=True)
        self._sliding_window_script = self._r.register_script(self._SLIDING_WINDOW_LUA)

    def allow(self, key: str, limit: int, window: int) -> bool:
        try:
            now = time.time()
            # 唯一 member 防止同一时间戳的请求被 ZADD 合并
            member = f"{now}:{uuid.uuid4().hex}"
            result = int(self._sliding_window_script(
                keys=[key], args=[now, window, limit, member]
            ))
            return result == 1
        except Exception:
            logger.error("Redis state store error", exc_info=True)
            return False

    def incr(self, key: str, by: int = 1) -> int:
        try:
            return int(self._r.incr(key, by))
        except Exception:
            logger.error("Redis state store error", exc_info=True)
            return 0

    def incr_float(self, key: str, by: float) -> float:
        try:
            return float(self._r.incrbyfloat(key, by))
        except Exception:
            logger.error("Redis state store error", exc_info=True)
            return 0.0

    def get(self, key: str) -> float:
        try:
            val = self._r.get(key)
            return float(val) if val is not None else 0.0
        except Exception:
            logger.error("Redis state store error", exc_info=True)
            return 0.0

    def keys(self, prefix: str) -> List[str]:
        try:
            return list(self._r.scan_iter(match=f"{prefix}*"))
        except Exception:
            logger.error("Redis state store error", exc_info=True)
            return []

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass


_store: Optional[StateStore] = None
_store_lock = threading.Lock()


def get_state_store() -> StateStore:
    """根据配置返回状态后端（懒初始化 + 线程安全）"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                if settings.state_backend == "redis":
                    _store = RedisStateStore(settings.redis_url)
                else:
                    _store = MemoryStateStore()
    return _store

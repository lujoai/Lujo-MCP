"""存储工厂 —— 根据配置自动选择后端"""

import logging

from app.config import settings
from app.runtime.core.storage.base import TraceStorage, SessionStorage, ErrorStorage, SpecStorage

logger = logging.getLogger(__name__)

# 合法后端白名单（大小写敏感）
_VALID_BACKENDS = {"memory", "postgresql"}

_trace_store: TraceStorage = None   # type: ignore
_session_store: SessionStorage = None  # type: ignore
_error_store: ErrorStorage = None  # type: ignore
_spec_store: SpecStorage = None  # type: ignore


def _validate_backend() -> None:
    """校验 storage_backend 配置值，非法值 fail-fast。

    防止拼写错误（如 "postgrsql"）静默回退到 memory，导致生产环境
    误以为用了 PG 实际用了内存，重启即丢数据。
    """
    if settings.storage_backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Invalid STORAGE_BACKEND={settings.storage_backend!r}. "
            f"Valid values: {sorted(_VALID_BACKENDS)}. "
            f"Check .env or environment variable spelling (case-sensitive)."
        )


def _raise_async_mix(feature: str) -> None:
    """FIX: P1-4 pg_async_enabled=True 时同步 getter fail-fast。

    asyncpg store 的方法均为 async，同步调用（不带 await）不会执行函数体、
    只会返回 coroutine 对象导致数据静默丢失。开启 pg_async_enabled 后，
    同步存储用户必须走 async 版本，否则启动/首次调用即抛配置错误，
    杜绝"部分丢部分写"的混合行为。
    """
    raise RuntimeError(
        f"{feature}: pg_async_enabled=True 要求全链路 async 调用，"
        f"同步 getter 不可用（防止 coroutine 未 await 导致数据静默丢失）。"
        f"请把调用链迁移到 async 版本，或设置 PG_ASYNC_ENABLED=false 保持同步行为。"
    )


def get_trace_store() -> TraceStorage:
    global _trace_store
    if _trace_store is None:
        _validate_backend()
        if settings.storage_backend == "postgresql":
            try:
                # Phase 3.1：feature flag 开启时走 asyncpg 异步实现（与 psycopg2 同步并存）
                if settings.pg_async_enabled:
                    # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                    _raise_async_mix("trace_store")
                else:
                    from app.runtime.core.storage.pg_store import PGTraceStore
                    _trace_store = PGTraceStore()
                    logger.info(
                        "trace_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                        settings.storage_backend,
                    )
            except Exception as e:
                if settings.storage_fallback_to_memory:
                    logger.warning("PG trace_store 初始化失败，降级到 memory: %s", e)
                    from app.runtime.core.storage.memory_store import MemoryTraceStore
                    _trace_store = MemoryTraceStore(max_entries=settings.memory_store_max_entries)
                    logger.warning("trace_store 已降级到 memory (fallback enabled)")
                else:
                    raise
        else:
            from app.runtime.core.storage.memory_store import MemoryTraceStore
            _trace_store = MemoryTraceStore(max_entries=settings.memory_store_max_entries)
            logger.info("trace_store initialized: backend=%s", settings.storage_backend)
    return _trace_store


def get_session_store() -> SessionStorage:
    global _session_store
    if _session_store is None:
        _validate_backend()
        if settings.storage_backend == "postgresql":
            try:
                # Phase 3.1：feature flag 开启时走 asyncpg 异步实现（与 psycopg2 同步并存）
                if settings.pg_async_enabled:
                    # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                    _raise_async_mix("session_store")
                else:
                    from app.runtime.core.storage.pg_store import PGSessionStore
                    _session_store = PGSessionStore()
                    logger.info(
                        "session_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                        settings.storage_backend,
                    )
            except Exception as e:
                if settings.storage_fallback_to_memory:
                    logger.warning("PG session_store 初始化失败，降级到 memory: %s", e)
                    from app.runtime.core.storage.memory_store import MemorySessionStore
                    _session_store = MemorySessionStore()
                    logger.warning("session_store 已降级到 memory (fallback enabled)")
                else:
                    raise
        else:
            from app.runtime.core.storage.memory_store import MemorySessionStore
            _session_store = MemorySessionStore()
            logger.info("session_store initialized: backend=%s", settings.storage_backend)
    return _session_store


def get_error_store() -> ErrorStorage:
    """返回错误存储实例（方案 C：按后端分发，PG 真实持久化，memory no-op）。"""
    global _error_store
    if _error_store is None:
        _validate_backend()
        if settings.storage_backend == "postgresql":
            try:
                if settings.pg_async_enabled:
                    # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                    _raise_async_mix("error_store")
                else:
                    from app.runtime.core.storage.pg_store import PGErrorStore
                    _error_store = PGErrorStore()
                    logger.info(
                        "error_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                        settings.storage_backend,
                    )
            except Exception as e:
                if settings.storage_fallback_to_memory:
                    logger.warning("PG error_store 初始化失败，降级到 no-op: %s", e)
                    from app.runtime.core.storage.noop_store import NoOpErrorStore
                    _error_store = NoOpErrorStore()
                    logger.warning("error_store 已降级到 no-op (fallback enabled)")
                else:
                    raise
        else:
            from app.runtime.core.storage.noop_store import NoOpErrorStore
            _error_store = NoOpErrorStore()
            logger.info("error_store initialized: backend=%s (no-op)", settings.storage_backend)
    return _error_store


def get_spec_store() -> SpecStorage:
    """返回规范存储实例（方案 C：按后端分发，PG 真实持久化，memory no-op）。"""
    global _spec_store
    if _spec_store is None:
        _validate_backend()
        if settings.storage_backend == "postgresql":
            try:
                if settings.pg_async_enabled:
                    # FIX: P1-4 同步 getter 遇到 async 后端 fail-fast（fallback=True 时由 except 分支降级）
                    _raise_async_mix("spec_store")
                else:
                    from app.runtime.core.storage.pg_store import PGSpecStore
                    _spec_store = PGSpecStore()
                    logger.info(
                        "spec_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                        settings.storage_backend,
                    )
            except Exception as e:
                if settings.storage_fallback_to_memory:
                    logger.warning("PG spec_store 初始化失败，降级到 no-op: %s", e)
                    from app.runtime.core.storage.noop_store import NoOpSpecStore
                    _spec_store = NoOpSpecStore()
                    logger.warning("spec_store 已降级到 no-op (fallback enabled)")
                else:
                    raise
        else:
            from app.runtime.core.storage.noop_store import NoOpSpecStore
            _spec_store = NoOpSpecStore()
            logger.info("spec_store initialized: backend=%s (no-op)", settings.storage_backend)
    return _spec_store

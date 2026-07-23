"""存储工厂 —— 根据配置自动选择后端"""

import logging

from app.config import settings
from app.mcp.core.storage.base import TraceStorage, SessionStorage

logger = logging.getLogger(__name__)

# 合法后端白名单（大小写敏感）
_VALID_BACKENDS = {"memory", "postgresql"}

_trace_store: TraceStorage = None   # type: ignore
_session_store: SessionStorage = None  # type: ignore


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


def get_trace_store() -> TraceStorage:
    global _trace_store
    if _trace_store is None:
        _validate_backend()
        if settings.storage_backend == "postgresql":
            # Phase 3.1：feature flag 开启时走 asyncpg 异步实现（与 psycopg2 同步并存）
            if settings.pg_async_enabled:
                from app.mcp.core.storage.async_pg_store import AsyncPGTraceStore
                _trace_store = AsyncPGTraceStore()
                logger.info(
                    "trace_store initialized: backend=%s, async=enabled (asyncpg)",
                    settings.storage_backend,
                )
            else:
                from app.mcp.core.storage.pg_store import PGTraceStore
                _trace_store = PGTraceStore()
                logger.info(
                    "trace_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                    settings.storage_backend,
                )
        else:
            from app.mcp.core.storage.memory_store import MemoryTraceStore
            _trace_store = MemoryTraceStore(max_entries=settings.memory_store_max_entries)
            logger.info("trace_store initialized: backend=%s", settings.storage_backend)
    return _trace_store


def get_session_store() -> SessionStorage:
    global _session_store
    if _session_store is None:
        _validate_backend()
        if settings.storage_backend == "postgresql":
            # Phase 3.1：feature flag 开启时走 asyncpg 异步实现（与 psycopg2 同步并存）
            if settings.pg_async_enabled:
                from app.mcp.core.storage.async_pg_store import AsyncPGSessionStore
                _session_store = AsyncPGSessionStore()
                logger.info(
                    "session_store initialized: backend=%s, async=enabled (asyncpg)",
                    settings.storage_backend,
                )
            else:
                from app.mcp.core.storage.pg_store import PGSessionStore
                _session_store = PGSessionStore()
                logger.info(
                    "session_store initialized: backend=%s, async=disabled (psycopg2 sync)",
                    settings.storage_backend,
                )
        else:
            from app.mcp.core.storage.memory_store import MemorySessionStore
            _session_store = MemorySessionStore()
            logger.info("session_store initialized: backend=%s", settings.storage_backend)
    return _session_store

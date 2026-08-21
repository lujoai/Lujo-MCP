"""单元测试：存储层优雅降级（P3-5）—— PG 初始化失败时自动降级到 memory"""

import pytest
import logging

from app.runtime.core.storage.memory_store import MemoryTraceStore, MemorySessionStore


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """每个测试前重置 factory 全局缓存，避免缓存污染"""
    import app.runtime.core.storage.factory as f
    f._trace_store = None
    f._session_store = None
    yield
    f._trace_store = None
    f._session_store = None


class TestTraceStoreFallback:
    """P3-5：trace_store 优雅降级"""

    def test_fallback_to_memory_when_pg_unavailable(self, monkeypatch, caplog):
        """PG 初始化失败 + fallback=True → 降级到 MemoryTraceStore"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        def _boom(self):
            raise RuntimeError("模拟 PG 连接失败")

        monkeypatch.setattr("app.runtime.core.storage.pg_trace_store.PGTraceStore.__init__", _boom)

        with caplog.at_level(logging.WARNING):
            from app.runtime.core.storage.factory import get_trace_store
            store = get_trace_store()

        assert isinstance(store, MemoryTraceStore)
        assert any("PG trace_store 初始化失败" in r.message for r in caplog.records)
        assert any("trace_store 已降级到 memory" in r.message for r in caplog.records)

    def test_fail_fast_when_fallback_disabled(self, monkeypatch):
        """PG 初始化失败 + fallback=False → 异常向上抛（fail-fast）"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)

        def _boom(self):
            raise RuntimeError("模拟 PG 连接失败")

        monkeypatch.setattr("app.runtime.core.storage.pg_trace_store.PGTraceStore.__init__", _boom)

        from app.runtime.core.storage.factory import get_trace_store
        with pytest.raises(RuntimeError, match="模拟 PG 连接失败"):
            get_trace_store()

    def test_memory_backend_not_affected(self, monkeypatch):
        """storage_backend=memory 时不受 fallback 逻辑影响"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        from app.runtime.core.storage.factory import get_trace_store
        store = get_trace_store()

        assert isinstance(store, MemoryTraceStore)


class TestSessionStoreFallback:
    """P3-5：session_store 优雅降级"""

    def test_session_store_fallback_to_memory(self, monkeypatch, caplog):
        """PG 初始化失败 + fallback=True → 降级到 MemorySessionStore"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        def _boom(self):
            raise RuntimeError("模拟 PG 连接失败")

        monkeypatch.setattr("app.runtime.core.storage.pg_session_store.PGSessionStore.__init__", _boom)

        with caplog.at_level(logging.WARNING):
            from app.runtime.core.storage.factory import get_session_store
            store = get_session_store()

        assert isinstance(store, MemorySessionStore)
        assert any("PG session_store 初始化失败" in r.message for r in caplog.records)
        assert any("session_store 已降级到 memory" in r.message for r in caplog.records)

    def test_session_store_fail_fast_when_fallback_disabled(self, monkeypatch):
        """PG 初始化失败 + fallback=False → 异常向上抛（fail-fast）"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)

        def _boom(self):
            raise RuntimeError("模拟 PG 连接失败")

        monkeypatch.setattr("app.runtime.core.storage.pg_session_store.PGSessionStore.__init__", _boom)

        from app.runtime.core.storage.factory import get_session_store
        with pytest.raises(RuntimeError, match="模拟 PG 连接失败"):
            get_session_store()

    def test_session_memory_backend_not_affected(self, monkeypatch):
        """storage_backend=memory 时 session_store 不受 fallback 逻辑影响"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        from app.runtime.core.storage.factory import get_session_store
        store = get_session_store()

        assert isinstance(store, MemorySessionStore)


@pytest.mark.asyncio
async def test_async_pg_session_store_does_not_mutate_caller_dict(monkeypatch):
    """验证 AsyncPGSessionStore.save() 拷贝 dict，不原地改写调用方传入的字典对象。"""
    from app.runtime.core.storage.async_pg_store import AsyncPGSessionStore
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("app.config.settings.pg_async_enabled", True)
    monkeypatch.setattr("app.runtime.core.storage.async_pg_store._ensure_init", AsyncMock())

    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None

    monkeypatch.setattr("app.runtime.core.storage.async_pg_store._get_pool", AsyncMock(return_value=mock_pool))

    store = AsyncPGSessionStore()
    orig_data = {"user": "alice", "created_at": 1000.0}
    data_copy = dict(orig_data)

    await store.save("sess-1", orig_data)

    assert "last_active" not in orig_data, "save() 不应原地改写调用方的传入字典"
    assert orig_data == data_copy


def test_async_pg_trace_store_write_counter_initialized():
    """验证 AsyncPGTraceStore._write_counter 在 __init__ 中显式初始化，无 hasattr 竞态。"""
    from app.runtime.core.storage.async_pg_store import AsyncPGTraceStore
    store = AsyncPGTraceStore()
    assert hasattr(store, "_write_counter")
    assert store._write_counter == 0

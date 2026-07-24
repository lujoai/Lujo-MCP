"""单元测试：存储层优雅降级（P3-5）—— PG 初始化失败时自动降级到 memory"""

import pytest
import logging

from app.mcp.core.storage.memory_store import MemoryTraceStore, MemorySessionStore


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """每个测试前重置 factory 全局缓存，避免缓存污染"""
    import app.mcp.core.storage.factory as f
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

        monkeypatch.setattr("app.mcp.core.storage.pg_store.PGTraceStore.__init__", _boom)

        with caplog.at_level(logging.WARNING):
            from app.mcp.core.storage.factory import get_trace_store
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

        monkeypatch.setattr("app.mcp.core.storage.pg_store.PGTraceStore.__init__", _boom)

        from app.mcp.core.storage.factory import get_trace_store
        with pytest.raises(RuntimeError, match="模拟 PG 连接失败"):
            get_trace_store()

    def test_memory_backend_not_affected(self, monkeypatch):
        """storage_backend=memory 时不受 fallback 逻辑影响"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        from app.mcp.core.storage.factory import get_trace_store
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

        monkeypatch.setattr("app.mcp.core.storage.pg_store.PGSessionStore.__init__", _boom)

        with caplog.at_level(logging.WARNING):
            from app.mcp.core.storage.factory import get_session_store
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

        monkeypatch.setattr("app.mcp.core.storage.pg_store.PGSessionStore.__init__", _boom)

        from app.mcp.core.storage.factory import get_session_store
        with pytest.raises(RuntimeError, match="模拟 PG 连接失败"):
            get_session_store()

    def test_session_memory_backend_not_affected(self, monkeypatch):
        """storage_backend=memory 时 session_store 不受 fallback 逻辑影响"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        from app.mcp.core.storage.factory import get_session_store
        store = get_session_store()

        assert isinstance(store, MemorySessionStore)

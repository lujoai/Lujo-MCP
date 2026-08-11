"""单元测试：存储工厂（后端校验 / error+spec 分发 / async fail-fast）"""

import logging

import pytest

import app.runtime.core.storage.factory as f
from app.runtime.core.storage.memory_store import MemoryTraceStore
from app.runtime.core.storage.noop_store import NoOpErrorStore, NoOpSpecStore


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """每个用例前后重置 factory 全局缓存，避免缓存污染"""
    for attr in ("_trace_store", "_session_store", "_error_store", "_spec_store"):
        setattr(f, attr, None)
    yield
    for attr in ("_trace_store", "_session_store", "_error_store", "_spec_store"):
        setattr(f, attr, None)


class TestValidateBackend:
    def test_invalid_backend_raises(self, monkeypatch):
        """拼写错误的 backend 应 fail-fast，而非静默回退 memory"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgrsql")
        with pytest.raises(ValueError, match="Invalid STORAGE_BACKEND"):
            f.get_trace_store()

    def test_valid_memory_backend(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        assert isinstance(f.get_trace_store(), MemoryTraceStore)


class TestErrorSpecStore:
    def test_memory_error_store_noop(self, monkeypatch):
        """memory 后端 error store 为 no-op 实现"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        assert isinstance(f.get_error_store(), NoOpErrorStore)

    def test_memory_spec_store_noop(self, monkeypatch):
        """memory 后端 spec store 为 no-op 实现"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        assert isinstance(f.get_spec_store(), NoOpSpecStore)

    def test_async_mix_error_store_fail_fast(self, monkeypatch):
        """pg_async_enabled=True 时同步 getter 应 fail-fast，防止 coroutine 静默丢失"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", True)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)
        with pytest.raises(RuntimeError, match="pg_async_enabled=True"):
            f.get_error_store()

    def test_async_mix_spec_store_fail_fast(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", True)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)
        with pytest.raises(RuntimeError, match="pg_async_enabled=True"):
            f.get_spec_store()

    def test_pg_error_store_fallback_noop(self, monkeypatch, caplog):
        """PG 初始化失败 + fallback=True → error store 降级到 no-op"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        def _boom(self):
            raise RuntimeError("pg down")

        monkeypatch.setattr("app.runtime.core.storage.pg_store.PGErrorStore.__init__", _boom)

        with caplog.at_level(logging.WARNING):
            store = f.get_error_store()
        assert isinstance(store, NoOpErrorStore)
        assert any("error_store 初始化失败" in r.message for r in caplog.records)

    def test_pg_error_store_fail_fast_when_fallback_disabled(self, monkeypatch):
        """PG 初始化失败 + fallback=False → error store 异常向上抛"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)

        def _boom(self):
            raise RuntimeError("pg down")

        monkeypatch.setattr("app.runtime.core.storage.pg_store.PGErrorStore.__init__", _boom)

        with pytest.raises(RuntimeError, match="pg down"):
            f.get_error_store()

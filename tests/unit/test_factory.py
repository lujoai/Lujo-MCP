"""单元测试：存储工厂（后端校验 / trace+session+error+spec+kb 分发 / async fail-fast / fallback）"""

import logging
from unittest.mock import MagicMock

import pytest

import app.runtime.core.storage.factory as f
from app.runtime.core.storage.memory_store import MemorySessionStore, MemoryTraceStore
from app.runtime.core.storage.noop_store import (
    NoOpErrorStore,
    NoOpKnowledgeBaseStore,
    NoOpSpecStore,
)


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """每个用例前后重置 factory 全局缓存，避免缓存污染"""
    for attr in ("_trace_store", "_session_store", "_error_store", "_spec_store", "_knowledge_store"):
        setattr(f, attr, None)
    yield
    for attr in ("_trace_store", "_session_store", "_error_store", "_spec_store", "_knowledge_store"):
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
        assert isinstance(f.get_session_store(), MemorySessionStore)


class TestSingletonCaching:
    def test_getters_return_cached_instance(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        trace_1 = f.get_trace_store()
        trace_2 = f.get_trace_store()
        assert trace_1 is trace_2

        session_1 = f.get_session_store()
        session_2 = f.get_session_store()
        assert session_1 is session_2

        error_1 = f.get_error_store()
        error_2 = f.get_error_store()
        assert error_1 is error_2

        spec_1 = f.get_spec_store()
        spec_2 = f.get_spec_store()
        assert spec_1 is spec_2

        kb_1 = f.get_knowledge_store()
        kb_2 = f.get_knowledge_store()
        assert kb_1 is kb_2


class TestPostgresSuccess:
    def test_pg_trace_and_session_success(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)

        mock_pg_trace = MagicMock()
        mock_pg_session = MagicMock()
        mock_pg_kb = MagicMock()

        monkeypatch.setattr("app.runtime.core.storage.pg_trace_store.PGTraceStore", mock_pg_trace)
        monkeypatch.setattr("app.runtime.core.storage.pg_session_store.PGSessionStore", mock_pg_session)
        monkeypatch.setattr("app.runtime.core.storage.pg_kb_store.PGKnowledgeBaseStore", mock_pg_kb)

        assert f.get_trace_store() == mock_pg_trace.return_value
        assert f.get_session_store() == mock_pg_session.return_value
        assert f.get_knowledge_store() == mock_pg_kb.return_value


class TestErrorSpecKnowledgeStore:
    def test_memory_error_spec_kb_noop(self, monkeypatch):
        """memory 后端 error, spec, knowledge store 为 no-op 实现"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        assert isinstance(f.get_error_store(), NoOpErrorStore)
        assert isinstance(f.get_spec_store(), NoOpSpecStore)
        assert isinstance(f.get_knowledge_store(), NoOpKnowledgeBaseStore)

    def test_async_mix_fail_fast(self, monkeypatch):
        """pg_async_enabled=True 时同步 getter 应 fail-fast，防止 coroutine 静默丢失"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", True)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)

        with pytest.raises(RuntimeError, match="pg_async_enabled=True"):
            f.get_trace_store()
        with pytest.raises(RuntimeError, match="pg_async_enabled=True"):
            f.get_session_store()
        with pytest.raises(RuntimeError, match="pg_async_enabled=True"):
            f.get_error_store()
        with pytest.raises(RuntimeError, match="pg_async_enabled=True"):
            f.get_spec_store()
        with pytest.raises(RuntimeError, match="pg_async_enabled=True"):
            f.get_knowledge_store()

    def test_pg_stores_fallback_when_enabled(self, monkeypatch, caplog):
        """PG 初始化失败 + fallback=True → 降级到 memory / no-op"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        def _boom(self):
            raise RuntimeError("pg down")

        monkeypatch.setattr("app.runtime.core.storage.pg_trace_store.PGTraceStore.__init__", _boom)
        monkeypatch.setattr("app.runtime.core.storage.pg_session_store.PGSessionStore.__init__", _boom)
        monkeypatch.setattr("app.runtime.core.storage.pg_error_store.PGErrorStore.__init__", _boom)
        monkeypatch.setattr("app.runtime.core.storage.pg_spec_store.PGSpecStore.__init__", _boom)
        monkeypatch.setattr("app.runtime.core.storage.pg_kb_store.PGKnowledgeBaseStore.__init__", _boom)

        with caplog.at_level(logging.WARNING):
            trace_s = f.get_trace_store()
            session_s = f.get_session_store()
            error_s = f.get_error_store()
            spec_s = f.get_spec_store()
            kb_s = f.get_knowledge_store()

        assert isinstance(trace_s, MemoryTraceStore)
        assert isinstance(session_s, MemorySessionStore)
        assert isinstance(error_s, NoOpErrorStore)
        assert isinstance(spec_s, NoOpSpecStore)
        assert isinstance(kb_s, NoOpKnowledgeBaseStore)

    def test_pg_stores_fail_fast_when_fallback_disabled(self, monkeypatch):
        """PG 初始化失败 + fallback=False → 异常向上抛"""
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)

        def _boom(self):
            raise RuntimeError("pg down")

        monkeypatch.setattr("app.runtime.core.storage.pg_trace_store.PGTraceStore.__init__", _boom)
        with pytest.raises(RuntimeError, match="pg down"):
            f.get_trace_store()

        monkeypatch.setattr("app.runtime.core.storage.pg_session_store.PGSessionStore.__init__", _boom)
        with pytest.raises(RuntimeError, match="pg down"):
            f.get_session_store()

        monkeypatch.setattr("app.runtime.core.storage.pg_error_store.PGErrorStore.__init__", _boom)
        with pytest.raises(RuntimeError, match="pg down"):
            f.get_error_store()

        monkeypatch.setattr("app.runtime.core.storage.pg_spec_store.PGSpecStore.__init__", _boom)
        with pytest.raises(RuntimeError, match="pg down"):
            f.get_spec_store()

        monkeypatch.setattr("app.runtime.core.storage.pg_kb_store.PGKnowledgeBaseStore.__init__", _boom)
        with pytest.raises(RuntimeError, match="pg down"):
            f.get_knowledge_store()

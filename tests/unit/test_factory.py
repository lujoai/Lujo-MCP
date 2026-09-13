"""单元测试：存储工厂（后端闸门校验与拒绝 / trace+session+error+spec+kb 分发 / 单例缓存）"""

import logging

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


class TestRemovedBackendRejected:
    """WP3（Step 3 Breaking #1）：postgresql 不再是合法运行时后端。

    原 ``TestPostgresSuccess`` 断言「配 postgresql → 分发到 PG store」；闸门收口后
    该行为被正式移除，这里改为断言拒绝语义。PG store 模块本体保留到 WP5 才删，
    故本类只锁闸门，不锁 PG 实现。
    """

    _GETTERS = (
        "get_trace_store",
        "get_session_store",
        "get_error_store",
        "get_spec_store",
        "get_knowledge_store",
    )

    @pytest.mark.parametrize("getter", _GETTERS)
    def test_postgresql_rejected_at_every_getter(self, monkeypatch, getter):
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        with pytest.raises(f.StorageBackendRemovedError):
            getattr(f, getter)()


class TestErrorSpecKnowledgeStore:
    def test_memory_error_spec_kb_noop(self, monkeypatch):
        """memory 后端 error, spec, knowledge store 为 no-op 实现"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        assert isinstance(f.get_error_store(), NoOpErrorStore)
        assert isinstance(f.get_spec_store(), NoOpSpecStore)
        assert isinstance(f.get_knowledge_store(), NoOpKnowledgeBaseStore)

    def test_rejection_precedes_async_mix_check(self, monkeypatch):
        """闸门优先于 async-mix 检查：pg_async_enabled 任何取值都改变不了拒绝结果。

        原 ``test_async_mix_fail_fast`` 断言 postgresql + pg_async_enabled=True 时
        同步 getter 抛 ``_AsyncMixError``。WP3 后 ``_validate_backend()`` 先拒绝，
        ``_raise_async_mix`` 已不可经 STORAGE_BACKEND 抵达（它随 PG 分支留到 WP5）。
        """
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)
        for async_enabled in (True, False):
            monkeypatch.setattr("app.config.settings.pg_async_enabled", async_enabled)
            for getter in TestRemovedBackendRejected._GETTERS:
                with pytest.raises(f.StorageBackendRemovedError):
                    getattr(f, getter)()

    def test_rejection_not_downgraded_by_fallback_flag(self, monkeypatch, caplog):
        """拒绝不是「初始化失败」：storage_fallback_to_memory 两档都必须保持拒绝。

        原 ``test_pg_stores_fallback_when_enabled`` / ``_fail_fast_when_fallback_disabled``
        断言 PG 构造失败时按该开关降级或抛出；WP3 后 postgresql 根本走不到构造阶段。
        存活下来的不变量是 DEV_PLAN S3-2 的「不能静默回 memory」：降级日志一条也不许有。
        """
        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        for fallback in (True, False):
            monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", fallback)
            for attr in ("_trace_store", "_session_store", "_error_store",
                         "_spec_store", "_knowledge_store"):
                setattr(f, attr, None)
            with caplog.at_level(logging.WARNING):
                for getter in TestRemovedBackendRejected._GETTERS:
                    with pytest.raises(f.StorageBackendRemovedError):
                        getattr(f, getter)()
            assert [r for r in caplog.records if "降级" in r.getMessage()] == [], (
                f"fallback={fallback} 时拒绝被降级日志掩盖"
            )
            for attr in ("_trace_store", "_session_store", "_error_store",
                         "_spec_store", "_knowledge_store"):
                assert getattr(f, attr) is None


class TestConcurrentInitialization:
    def test_concurrent_get_stores_is_thread_safe(self, monkeypatch):
        """多线程高并发请求各个 store 时，double-checked locking 保证全局唯一单例且无竞态报错。"""
        from concurrent.futures import ThreadPoolExecutor
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")

        def _fetch_all(i):
            return (
                f.get_trace_store(),
                f.get_session_store(),
                f.get_error_store(),
                f.get_spec_store(),
                f.get_knowledge_store(),
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(_fetch_all, range(32)))

        first = results[0]
        for item in results[1:]:
            assert item[0] is first[0]
            assert item[1] is first[1]
            assert item[2] is first[2]
            assert item[3] is first[3]
            assert item[4] is first[4]

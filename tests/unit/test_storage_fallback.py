"""单元测试：存储层降级语义（P3-5 → WP3 收口；PG 开关随 WP6 删除）

当前仍然成立的两条不变量：

1. ``STORAGE_BACKEND=postgresql`` 的**拒绝**不是「初始化失败」，不得被任何
   降级逻辑吞成 memory/no-op（DEV_PLAN S3-2：不能静默回 memory；原
   ``storage_fallback_to_memory`` 开关已随 Step 3 WP6 删除）；
2. ``storage_backend=memory`` 完全不受降级逻辑影响（默认行为逐位不变）。

原先「PG 构造失败 → 按开关降级或抛出」的用例已无可达路径：闸门在构造之前就拒绝。
``get_knowledge_store()`` 自身的 SQLite → no-op 降级是**无条件 try/except**，由
``tests/unit/test_storage_backend_removal.py`` 与 ``test_sqlite_kb_store.py`` 覆盖。
"""

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
    """trace_store：拒绝不得被 fallback 吞掉"""

    def test_removed_backend_not_downgraded_when_fallback_enabled(self, monkeypatch, caplog):
        """postgresql + fallback=True → 仍然拒绝，不降级到 MemoryTraceStore"""
        from app.runtime.core.storage.factory import StorageBackendRemovedError, get_trace_store

        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(StorageBackendRemovedError):
                get_trace_store()

        assert [r for r in caplog.records if "降级" in r.getMessage()] == []

    def test_removed_backend_fails_fast_when_fallback_disabled(self, monkeypatch):
        """postgresql + fallback=False → 同样拒绝（开关不改变闸门结论）"""
        from app.runtime.core.storage.factory import StorageBackendRemovedError, get_trace_store

        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")

        with pytest.raises(StorageBackendRemovedError):
            get_trace_store()

    def test_memory_backend_not_affected(self, monkeypatch):
        """storage_backend=memory 时不受 fallback 逻辑影响"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")

        from app.runtime.core.storage.factory import get_trace_store
        store = get_trace_store()

        assert isinstance(store, MemoryTraceStore)


class TestSessionStoreFallback:
    """session_store：拒绝不得被 fallback 吞掉"""

    def test_session_store_removed_backend_not_downgraded(self, monkeypatch, caplog):
        """postgresql + fallback=True → 仍然拒绝，不降级到 MemorySessionStore"""
        from app.runtime.core.storage.factory import StorageBackendRemovedError, get_session_store

        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(StorageBackendRemovedError):
                get_session_store()

        assert [r for r in caplog.records if "降级" in r.getMessage()] == []

    def test_session_store_removed_backend_fails_fast(self, monkeypatch):
        """postgresql + fallback=False → 同样拒绝"""
        from app.runtime.core.storage.factory import StorageBackendRemovedError, get_session_store

        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")

        with pytest.raises(StorageBackendRemovedError):
            get_session_store()

    def test_session_memory_backend_not_affected(self, monkeypatch):
        """storage_backend=memory 时 session_store 不受 fallback 逻辑影响"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")

        from app.runtime.core.storage.factory import get_session_store
        store = get_session_store()

        assert isinstance(store, MemorySessionStore)


# ---------------------------------------------------------------------------
# FIX: R7-V4 → WP3 —— 配置错误 fail-fast，不允许被 fallback 吞
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("getter,attr", [
    ("get_trace_store", "_trace_store"),
    ("get_session_store", "_session_store"),
    ("get_error_store", "_error_store"),
    ("get_spec_store", "_spec_store"),
    ("get_knowledge_store", "_knowledge_store"),
])
def test_removed_backend_error_not_swallowed_by_fallback(monkeypatch, getter, attr):
    """postgresql（原降级开关开启场景，开关已随 WP6 删除）→ 同步 getter 必须抛拒绝异常，
    不得静默降级 memory（重启即丢）。

    原用例锁的是 ``_AsyncMixError`` 不被 fallback 吞掉；WP3 后闸门在 async-mix
    检查之前就拒绝，同一不变量由 ``StorageBackendRemovedError`` 承接。
    """
    import app.runtime.core.storage.factory as f

    setattr(f, attr, None)
    monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")

    with pytest.raises(f.StorageBackendRemovedError):
        getattr(f, getter)()

    # 单例不得被静默降级实例污染
    assert getattr(f, attr) is None
    setattr(f, attr, None)

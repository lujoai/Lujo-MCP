"""单元测试：存储层降级语义（P3-5 → WP3 收口）

WP3（Step 3 Breaking #1）后本文件的被测对象收窄为两条仍然成立的不变量：

1. ``STORAGE_BACKEND=postgresql`` 的**拒绝**不是「初始化失败」，
   ``storage_fallback_to_memory`` 两档都不得把它降级成 memory/no-op
   （DEV_PLAN S3-2：不能静默回 memory）；
2. ``storage_backend=memory`` 完全不受 fallback 逻辑影响（默认行为逐位不变）。

原先「PG 构造失败 → 按开关降级或抛出」的用例已无可达路径：闸门在构造之前就拒绝。
``get_knowledge_store()`` 自身的 SQLite → no-op 降级是**无条件 try/except、从不读
``storage_fallback_to_memory``**（设计文档 §6.1），由
``tests/unit/test_storage_backend_removal.py`` 与 ``test_sqlite_kb_store.py`` 覆盖。
文件末尾直接实例化 AsyncPG* 的用例不经工厂闸门，保留到 WP5 随 PG 模块一并删除。
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
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(StorageBackendRemovedError):
                get_trace_store()

        assert [r for r in caplog.records if "降级" in r.getMessage()] == []

    def test_removed_backend_fails_fast_when_fallback_disabled(self, monkeypatch):
        """postgresql + fallback=False → 同样拒绝（开关不改变闸门结论）"""
        from app.runtime.core.storage.factory import StorageBackendRemovedError, get_trace_store

        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)

        with pytest.raises(StorageBackendRemovedError):
            get_trace_store()

    def test_memory_backend_not_affected(self, monkeypatch):
        """storage_backend=memory 时不受 fallback 逻辑影响"""
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        from app.runtime.core.storage.factory import get_trace_store
        store = get_trace_store()

        assert isinstance(store, MemoryTraceStore)


class TestSessionStoreFallback:
    """session_store：拒绝不得被 fallback 吞掉"""

    def test_session_store_removed_backend_not_downgraded(self, monkeypatch, caplog):
        """postgresql + fallback=True → 仍然拒绝，不降级到 MemorySessionStore"""
        from app.runtime.core.storage.factory import StorageBackendRemovedError, get_session_store

        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(StorageBackendRemovedError):
                get_session_store()

        assert [r for r in caplog.records if "降级" in r.getMessage()] == []

    def test_session_store_removed_backend_fails_fast(self, monkeypatch):
        """postgresql + fallback=False → 同样拒绝"""
        from app.runtime.core.storage.factory import StorageBackendRemovedError, get_session_store

        monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
        monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", False)

        with pytest.raises(StorageBackendRemovedError):
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


# ---------------------------------------------------------------------------
# FIX: R7-V3 —— 归档失败后 ROLLBACK，过期清理不再永久停摆
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_pg_cleanup_expired_rolls_back_after_archive_failure(monkeypatch):
    """归档失败后必须 ROLLBACK 清理事务状态，紧接的 DELETE 才能正常执行。

    旧实现仅 warning：asyncpg 连接停留 failed transaction，DELETE FROM traces
    复用同连接必抛 → 每轮清理同位失败，过期清理永久停摆。
    """
    from unittest.mock import AsyncMock, MagicMock

    import app.runtime.core.storage.async_pg_store as apg

    executed = []

    class _RecordingConn(AsyncMock):
        async def execute(self, sql, *args, **kwargs):
            executed.append(sql)
            return "DELETE 3"

    conn = _RecordingConn()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None

    async def _archive_boom(_conn, _days):
        raise RuntimeError("archive table missing")

    monkeypatch.setattr(apg, "_ensure_init", AsyncMock())
    monkeypatch.setattr(apg, "_get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(apg, "_archive_old_traces", _archive_boom)
    monkeypatch.setattr("app.config.settings.pg_archive_enabled", True)

    store = apg.AsyncPGTraceStore()
    affected = await store.cleanup_expired(ttl_seconds=3600)

    assert affected == 3
    rollback_idx = next(
        i for i, sql in enumerate(executed) if sql.strip().upper() == "ROLLBACK"
    )
    delete_idx = next(i for i, sql in enumerate(executed) if "DELETE FROM traces" in sql)
    assert rollback_idx < delete_idx, "ROLLBACK 必须先于 DELETE 清理 failed transaction"


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
    """postgresql + storage_fallback_to_memory=True（默认）→ 同步 getter 必须抛拒绝异常，
    不得静默降级 memory（重启即丢）。

    原用例锁的是 ``_AsyncMixError`` 不被 fallback 吞掉；WP3 后闸门在 async-mix
    检查之前就拒绝，同一不变量由 ``StorageBackendRemovedError`` 承接。
    """
    import app.runtime.core.storage.factory as f

    setattr(f, attr, None)
    monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
    monkeypatch.setattr("app.config.settings.pg_async_enabled", True)
    monkeypatch.setattr("app.config.settings.storage_fallback_to_memory", True)

    with pytest.raises(f.StorageBackendRemovedError):
        getattr(f, getter)()

    # 单例不得被静默降级实例污染
    assert getattr(f, attr) is None
    setattr(f, attr, None)

"""单元测试：存储层"""

import pytest
import time

from app.runtime.core.storage.memory_store import MemoryTraceStore, MemorySessionStore
from app.runtime.core.storage import factory as factory_mod


# ════════════════════════════════════════════
#  内存存储测试（始终可用）
# ════════════════════════════════════════════

class TestMemoryTraceStore:

    def setup_method(self):
        self.store = MemoryTraceStore()

    def test_save_and_get(self):
        self.store.save_entry("rid-1", {"timestamp": 1.0, "step": "start", "data": {"a": 1}})
        self.store.save_entry("rid-1", {"timestamp": 2.0, "step": "end", "data": {"b": 2}})

        entries = self.store.get_entries("rid-1")
        assert len(entries) == 2
        assert entries[0]["step"] == "start"
        assert entries[1]["step"] == "end"

    def test_delete(self):
        self.store.save_entry("rid-2", {"timestamp": 1.0, "step": "test", "data": None})
        self.store.delete("rid-2")
        assert self.store.get_entries("rid-2") == []

    def test_cleanup_expired(self):
        self.store.save_entry("rid-old", {"timestamp": time.time() - 7200, "step": "old", "data": None})
        self.store.save_entry("rid-new", {"timestamp": time.time(), "step": "new", "data": None})

        count = self.store.cleanup_expired(ttl_seconds=3600)
        assert count == 1
        assert self.store.get_entries("rid-old") == []
        assert len(self.store.get_entries("rid-new")) == 1

    def test_cleanup_expired_tolerates_entry_without_timestamp(self):
        """W13 / P4：缺 timestamp 的条目不得让整轮 TTL 清理抛 KeyError。

        清理是周期性后台任务（app/main.py 的 periodic_cleanup），它一抛异常就
        等于内存只增不减 —— 唯一剩下的约束只有 max_entries 的 FIFO。
        无法定龄的条目按「不清理」处理：宁可多留，不可误删用户现场。
        """
        self.store.save_entry("rid-no-ts", {"step": "legacy", "data": None})
        self.store.save_entry("rid-old", {"timestamp": time.time() - 7200, "step": "old", "data": None})
        self.store.save_entry("rid-new", {"timestamp": time.time(), "step": "new", "data": None})

        count = self.store.cleanup_expired(ttl_seconds=3600)

        assert count == 1, "只应清掉能定龄且已过期的那条"
        assert self.store.get_entries("rid-old") == []
        assert len(self.store.get_entries("rid-new")) == 1
        assert len(self.store.get_entries("rid-no-ts")) == 1, "无法定龄的条目不得被误删"

    def test_list_request_ids_sorted_by_last_entry_timestamp(self):
        self.store.save_entry("rid-old", {"timestamp": 10.0, "step": "start", "data": None})
        self.store.save_entry("rid-new", {"timestamp": 20.0, "step": "start", "data": None})
        self.store.save_entry("rid-old", {"timestamp": 30.0, "step": "end", "data": None})

        assert self.store.list_request_ids(limit=10) == ["rid-old", "rid-new"]

    def test_entries_per_request_capped(self, monkeypatch):
        """FIX b8-4: 单 request_id 条目数受上限约束（此前仅 request_id 数量有上限）。"""
        monkeypatch.setattr(MemoryTraceStore, "_MAX_ENTRIES_PER_REQUEST", 5)
        store = MemoryTraceStore()
        for i in range(10):
            store.save_entry("rid-capped", {"timestamp": float(i), "step": str(i), "data": None})

        entries = store.get_entries("rid-capped")
        # 保留最新 5 条（丢最旧），有界
        assert len(entries) == 5
        assert [e["step"] for e in entries] == ["5", "6", "7", "8", "9"]

    def test_batch_entries_per_request_capped(self, monkeypatch):
        """FIX b8-4: 批量写入同样受单 request_id 上限约束。"""
        monkeypatch.setattr(MemoryTraceStore, "_MAX_ENTRIES_PER_REQUEST", 5)
        store = MemoryTraceStore()
        store.save_entries("rid-batch", [{"timestamp": float(i), "step": str(i), "data": None} for i in range(8)])

        entries = store.get_entries("rid-batch")
        assert len(entries) == 5
        assert [e["step"] for e in entries] == ["3", "4", "5", "6", "7"]


class TestMemorySessionStore:

    def setup_method(self):
        self.store = MemorySessionStore()

    def test_save_get_delete(self):
        self.store.save("s-1", {"session_id": "s-1", "created_at": time.time(), "metadata": {}})
        s = self.store.get("s-1")
        assert s is not None
        assert s["session_id"] == "s-1"

        self.store.delete("s-1")
        assert self.store.get("s-1") is None

    def test_list_active(self):
        now = time.time()
        self.store.save("s-active", {"session_id": "s-active", "created_at": now, "last_active": now})
        self.store._store["s-stale"] = {
            "session_id": "s-stale",
            "created_at": now - 7200,
            "last_active": now - 7200,
        }

        active = self.store.list_active(ttl_seconds=3600)
        assert len(active) == 1, f"Expected 1 active, got: {active}"
        assert active[0]["session_id"] == "s-active"


# ════════════════════════════════════════════
#  存储工厂测试（M1：拼写错误 fail-fast）
# ════════════════════════════════════════════

class TestStorageFactory:
    """校验 factory 对 storage_backend 的白名单约束与 fail-fast 行为。

    覆盖 P1 M1：防止 STORAGE_BACKEND 拼写错误（如 "postgrsql"）静默
    回退到 memory，导致生产环境数据丢失。
    """

    def setup_method(self):
        # 每个用例前清空 factory 单例缓存，避免跨用例污染
        factory_mod._trace_store = None
        factory_mod._session_store = None

    def teardown_method(self):
        # 用例结束后也清空，避免影响后续 PG 测试或其它单测
        factory_mod._trace_store = None
        factory_mod._session_store = None

    def test_valid_memory_returns_memory_store(self, monkeypatch):
        """合法值 'memory' → 返回 MemoryTraceStore / MemorySessionStore 实例"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "memory")

        ts = factory_mod.get_trace_store()
        ss = factory_mod.get_session_store()

        assert isinstance(ts, MemoryTraceStore)
        assert isinstance(ss, MemorySessionStore)

    def test_postgresql_is_rejected_not_routed(self, monkeypatch):
        """WP3（Step 3 Breaking #1）：'postgresql' 不再是合法值 → 拒绝，且不误建 memory store。

        原用例断言「配 postgresql → 走 PG 分支」；闸门收口后该分发被正式移除。
        保留原有的 memory spy 装置：它锁住的「不得静默回退 memory」在拒绝语义下
        依然是核心不变量（DEV_PLAN S3-2）。PG store 模块本体保留到 WP5 才删。
        """
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "postgresql")

        # 在 memory_store 上加 spy，监控拒绝路径是否偷偷实例化降级 store
        mem_trace_calls = []
        mem_session_calls = []

        class _SpyMemoryTraceStore(MemoryTraceStore):
            def __init__(self):
                mem_trace_calls.append(True)
                super().__init__()

        class _SpyMemorySessionStore(MemorySessionStore):
            def __init__(self):
                mem_session_calls.append(True)
                super().__init__()

        import app.runtime.core.storage.memory_store as mem_mod
        monkeypatch.setattr(mem_mod, "MemoryTraceStore", _SpyMemoryTraceStore)
        monkeypatch.setattr(mem_mod, "MemorySessionStore", _SpyMemorySessionStore)

        with pytest.raises(factory_mod.StorageBackendRemovedError):
            factory_mod.get_trace_store()
        with pytest.raises(factory_mod.StorageBackendRemovedError):
            factory_mod.get_session_store()

        assert mem_trace_calls == [], "拒绝路径误建 MemoryTraceStore（静默回退）"
        assert mem_session_calls == [], "拒绝路径误建 MemorySessionStore（静默回退）"

    def test_invalid_backend_raises_valueerror(self, monkeypatch):
        """拼写错误 'postgrsql'（少一个 s）→ 抛 ValueError"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "postgrsql")

        with pytest.raises(ValueError) as exc_info:
            factory_mod.get_trace_store()

        msg = str(exc_info.value)
        assert "postgrsql" in msg
        # A1（WP3）：白名单收窄后有效值列表只剩 memory；"postgresql" 只允许以
        # 「已移除」提示的身份出现，不得再被当成合法选项广告出去。
        assert "Valid values: ['memory']" in msg
        assert "postgresql" in msg and "移除" in msg
        assert "case-sensitive" in msg or "spelling" in msg

    def test_empty_backend_raises_valueerror(self, monkeypatch):
        """空串 '' → 抛 ValueError（!r 在错误信息中显示为 ''）"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "")

        with pytest.raises(ValueError):
            factory_mod.get_trace_store()

    def test_case_sensitive_raises_valueerror(self, monkeypatch):
        """大小写错误 'PostgreSQL' → 抛 ValueError。

        白名单严格大小写敏感，避免 'PostgreSQL' / 'POSTGRESQL' 等变体
        造成歧义。错误信息中明确提示 case-sensitive。
        """
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "PostgreSQL")

        with pytest.raises(ValueError) as exc_info:
            factory_mod.get_trace_store()

        assert "case-sensitive" in str(exc_info.value)

class TestErrorSpecFactory:
    """校验 factory 对 ErrorStorage / SpecStorage 的后端分发（方案 C）。

    - memory 后端 → NoOpErrorStore / NoOpSpecStore（no-op 保接口一致）
    - postgresql → WP3 起被闸门拒绝（原「分发到 PG 实现」已移除；PG 模块留到 WP5 删）
    """

    def setup_method(self):
        factory_mod._error_store = None
        factory_mod._spec_store = None

    def teardown_method(self):
        factory_mod._error_store = None
        factory_mod._spec_store = None

    def test_memory_returns_noop_stores(self, monkeypatch):
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "memory")

        from app.runtime.core.storage.noop_store import NoOpErrorStore, NoOpSpecStore
        es = factory_mod.get_error_store()
        ss = factory_mod.get_spec_store()

        assert isinstance(es, NoOpErrorStore)
        assert isinstance(ss, NoOpSpecStore)

    def test_postgresql_is_rejected_not_routed(self, monkeypatch):
        """WP3：配置 postgresql → error/spec getter 拒绝，且不误建 no-op 降级 store。"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "postgresql")

        import app.runtime.core.storage.noop_store as noop_mod

        noop_calls = []
        class _SpyNoOpErrorStore:
            def __init__(self): noop_calls.append("error")
        class _SpyNoOpSpecStore:
            def __init__(self): noop_calls.append("spec")

        monkeypatch.setattr(noop_mod, "NoOpErrorStore", _SpyNoOpErrorStore)
        monkeypatch.setattr(noop_mod, "NoOpSpecStore", _SpyNoOpSpecStore)

        with pytest.raises(factory_mod.StorageBackendRemovedError):
            factory_mod.get_error_store()
        with pytest.raises(factory_mod.StorageBackendRemovedError):
            factory_mod.get_spec_store()

        assert noop_calls == [], "拒绝路径误建 no-op store（静默降级）"


class TestNoOpStores:
    """校验 no-op 实现的零行为语义（memory 后端契约对齐）。"""

    def test_noop_error_store(self):
        from app.runtime.core.storage.noop_store import NoOpErrorStore
        es = NoOpErrorStore()
        assert es.upsert_error({"error_id": "e1"}) is None

    def test_noop_spec_store(self):
        from app.runtime.core.storage.noop_store import NoOpSpecStore
        ss = NoOpSpecStore()
        assert ss.save_spec({"id": "s1"}) is None
        assert ss.get_spec("s1") is None
        assert ss.list_specs() == []
        assert ss.delete_spec("s1") is False


class TestErrorSpecABCContract:
    """校验 ErrorStorage / SpecStorage ABC 为抽象契约，且各后端实现一致。"""

    def test_abc_is_abstract(self):
        from app.runtime.core.storage.base import ErrorStorage, SpecStorage
        import inspect
        assert inspect.isabstract(ErrorStorage)
        assert inspect.isabstract(SpecStorage)
        # 抽象方法契约存在
        assert "upsert_error" in ErrorStorage.__abstractmethods__
        for m in ("save_spec", "get_spec", "list_specs", "delete_spec"):
            assert m in SpecStorage.__abstractmethods__

    def test_noop_stores_implement_abc(self):
        from app.runtime.core.storage.noop_store import NoOpErrorStore, NoOpSpecStore
        from app.runtime.core.storage.base import ErrorStorage, SpecStorage
        assert isinstance(NoOpErrorStore(), ErrorStorage)
        assert isinstance(NoOpSpecStore(), SpecStorage)

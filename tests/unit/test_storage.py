"""单元测试：存储层"""
import pytest
import time

from app.mcp.core.storage.memory_store import MemoryTraceStore, MemorySessionStore
from app.mcp.core.storage import factory as factory_mod


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

    def test_list_request_ids_sorted_by_last_entry_timestamp(self):
        self.store.save_entry("rid-old", {"timestamp": 10.0, "step": "start", "data": None})
        self.store.save_entry("rid-new", {"timestamp": 20.0, "step": "start", "data": None})
        self.store.save_entry("rid-old", {"timestamp": 30.0, "step": "end", "data": None})

        assert self.store.list_request_ids(limit=10) == ["rid-old", "rid-new"]


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
#  PostgreSQL 存储测试（需要 PG 运行，否则跳过）
# ════════════════════════════════════════════

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 未安装，跳过 PG 测试")

PG_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "ai_debug_mcp_test",
    "user": "postgres",
    "password": "",
}


def _pg_available() -> bool:
    """检测 PostgreSQL 是否可用"""
    try:
        conn = psycopg2.connect(**PG_CONFIG)
        conn.close()
        return True
    except Exception:
        return False


_pg_skip = not _pg_available()


@pytest.mark.skipif(_pg_skip, reason="PostgreSQL 不可用，跳过 PG 测试")
class TestPGTraceStore:

    def setup_method(self):
        from app.mcp.core.storage.pg_store import PGTraceStore
        # 临时覆盖连接参数
        import app.mcp.core.storage.pg_store as mod
        self._orig_pool = mod._pool
        self._orig_get_pool = mod._get_pool

        def _test_pool():
            return psycopg2.pool.ThreadedConnectionPool(2, 5, **PG_CONFIG)

        mod._get_pool = _test_pool
        mod._pool = None
        self.store = PGTraceStore()
        # 清理残留
        conn = mod._get_pool().getconn()
        conn.execute("DELETE FROM traces")
        conn.execute("DELETE FROM sessions")
        conn.commit()
        mod._get_pool().putconn(conn)

    def teardown_method(self):
        import app.mcp.core.storage.pg_store as mod
        if mod._pool:
            mod._pool.closeall()
            mod._pool = None
        mod._get_pool = self._orig_get_pool
        mod._pool = self._orig_pool

    def test_save_and_get(self):
        self.store.save_entry("rid-pg-1", {"timestamp": 1.0, "step": "start", "data": {"key": "val"}})
        self.store.save_entry("rid-pg-1", {"timestamp": 2.0, "step": "end", "data": None})

        entries = self.store.get_entries("rid-pg-1")
        assert len(entries) == 2
        assert entries[0]["step"] == "start"
        assert entries[0]["data"] == {"key": "val"}
        assert entries[1]["data"] is None

    def test_delete(self):
        self.store.save_entry("rid-pg-del", {"timestamp": 1.0, "step": "x", "data": None})
        self.store.delete("rid-pg-del")
        assert self.store.get_entries("rid-pg-del") == []

    def test_cleanup_expired(self):
        self.store.save_entry("rid-pg-old", {"timestamp": time.time() - 7200, "step": "old", "data": None})
        self.store.save_entry("rid-pg-new", {"timestamp": time.time(), "step": "new", "data": None})

        count = self.store.cleanup_expired(ttl_seconds=3600)
        assert count >= 1


@pytest.mark.skipif(_pg_skip, reason="PostgreSQL 不可用，跳过 PG 测试")
class TestPGSessionStore:

    def setup_method(self):
        from app.mcp.core.storage.pg_store import PGSessionStore
        import app.mcp.core.storage.pg_store as mod
        self._orig_pool = mod._pool
        self._orig_get_pool = mod._get_pool

        def _test_pool():
            return psycopg2.pool.ThreadedConnectionPool(2, 5, **PG_CONFIG)

        mod._get_pool = _test_pool
        mod._pool = None
        self.store = PGSessionStore()
        conn = mod._get_pool().getconn()
        conn.execute("DELETE FROM sessions")
        conn.commit()
        mod._get_pool().putconn(conn)

    def teardown_method(self):
        import app.mcp.core.storage.pg_store as mod
        if mod._pool:
            mod._pool.closeall()
            mod._pool = None
        mod._get_pool = self._orig_get_pool
        mod._pool = self._orig_pool

    def test_save_get_delete(self):
        now = time.time()
        self.store.save("s-pg-1", {"session_id": "s-pg-1", "created_at": now, "metadata": {"role": "test"}})
        s = self.store.get("s-pg-1")
        assert s is not None
        assert s["session_id"] == "s-pg-1"
        assert s["metadata"] == {"role": "test"}

        self.store.delete("s-pg-1")
        assert self.store.get("s-pg-1") is None

    def test_list_active(self):
        now = time.time()
        self.store.save("s-pg-active", {"session_id": "s-pg-active", "created_at": now, "metadata": {}})

        # 用 save 的 ON CONFLICT 写入过期 session
        import app.mcp.core.storage.pg_store as mod
        conn = mod._get_pool().getconn()
        conn.execute(
            "INSERT INTO sessions (session_id, created_at, last_active, metadata) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (session_id) DO UPDATE SET last_active = EXCLUDED.last_active",
            ("s-pg-stale", now - 7200, now - 7200, "{}"),
        )
        conn.commit()
        mod._get_pool().putconn(conn)

        active = self.store.list_active(ttl_seconds=3600)
        assert len(active) >= 1
        ids = [s["session_id"] for s in active]
        assert "s-pg-active" in ids
        assert "s-pg-stale" not in ids


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

    def test_valid_postgresql_routes_to_pg_store(self, monkeypatch):
        """合法值 'postgresql' → 走 PG 分支，不误回退 memory。

        用 stub 替换 PGTraceStore / PGSessionStore 避免真实连 PG；
        同时在 MemoryTraceStore / MemorySessionStore 上加 spy，
        断言调用次数为 0，防止"配 PG 但误回退 memory"的回归。
        """
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "postgresql")

        # stub PG store 类（避免真实连 PG 触发 _ensure_init）
        class _StubPGTraceStore:
            def __init__(self): pass
        class _StubPGSessionStore:
            def __init__(self): pass

        # 在 memory_store 上加 spy，监控是否被误实例化
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

        # factory 内部用延迟 import，需 patch 模块属性
        import app.mcp.core.storage.pg_store as pg_mod
        import app.mcp.core.storage.memory_store as mem_mod
        monkeypatch.setattr(pg_mod, "PGTraceStore", _StubPGTraceStore)
        monkeypatch.setattr(pg_mod, "PGSessionStore", _StubPGSessionStore)
        monkeypatch.setattr(mem_mod, "MemoryTraceStore", _SpyMemoryTraceStore)
        monkeypatch.setattr(mem_mod, "MemorySessionStore", _SpyMemorySessionStore)

        ts = factory_mod.get_trace_store()
        ss = factory_mod.get_session_store()

        assert isinstance(ts, _StubPGTraceStore)
        assert isinstance(ss, _StubPGSessionStore)
        assert mem_trace_calls == [], "配置 postgresql 但误回退 MemoryTraceStore"
        assert mem_session_calls == [], "配置 postgresql 但误回退 MemorySessionStore"

    def test_invalid_backend_raises_valueerror(self, monkeypatch):
        """拼写错误 'postgrsql'（少一个 s）→ 抛 ValueError"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "postgrsql")

        with pytest.raises(ValueError) as exc_info:
            factory_mod.get_trace_store()

        msg = str(exc_info.value)
        assert "postgrsql" in msg
        assert "memory" in msg and "postgresql" in msg
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


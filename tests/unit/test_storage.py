"""单元测试：存储层"""
import json

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
        from app.runtime.core.storage.pg_trace_store import PGTraceStore
        # 临时覆盖连接参数
        import app.runtime.core.storage.pg_executor as mod
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
        import app.runtime.core.storage.pg_executor as mod
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
        from app.runtime.core.storage.pg_session_store import PGSessionStore
        import app.runtime.core.storage.pg_executor as mod
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
        import app.runtime.core.storage.pg_executor as mod
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
        import app.runtime.core.storage.pg_executor as mod
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
        import app.runtime.core.storage.pg_trace_store as pg_trace_mod
        import app.runtime.core.storage.pg_session_store as pg_session_mod
        import app.runtime.core.storage.memory_store as mem_mod
        monkeypatch.setattr(pg_trace_mod, "PGTraceStore", _StubPGTraceStore)
        monkeypatch.setattr(pg_session_mod, "PGSessionStore", _StubPGSessionStore)
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


class TestErrorSpecFactory:
    """校验 factory 对 ErrorStorage / SpecStorage 的后端分发（方案 C）。

    - memory 后端 → NoOpErrorStore / NoOpSpecStore（no-op 保接口一致）
    - postgresql 后端 → PG 实现（stub 替换避免真实连 PG）
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

    def test_postgresql_routes_to_pg_stores(self, monkeypatch):
        """配置 postgresql → 分发到 PG 实现，不误回退 no-op。"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "storage_backend", "postgresql")
        monkeypatch.setattr(_settings, "pg_async_enabled", False)

        class _StubPGErrorStore:
            def __init__(self): pass
        class _StubPGSpecStore:
            def __init__(self): pass

        import app.runtime.core.storage.pg_error_store as pg_error_mod
        import app.runtime.core.storage.pg_spec_store as pg_spec_mod
        import app.runtime.core.storage.noop_store as noop_mod

        noop_calls = []
        class _SpyNoOpErrorStore:
            def __init__(self): noop_calls.append("error")
        class _SpyNoOpSpecStore:
            def __init__(self): noop_calls.append("spec")

        monkeypatch.setattr(pg_error_mod, "PGErrorStore", _StubPGErrorStore)
        monkeypatch.setattr(pg_spec_mod, "PGSpecStore", _StubPGSpecStore)
        monkeypatch.setattr(noop_mod, "NoOpErrorStore", _SpyNoOpErrorStore)
        monkeypatch.setattr(noop_mod, "NoOpSpecStore", _SpyNoOpSpecStore)

        es = factory_mod.get_error_store()
        ss = factory_mod.get_spec_store()

        assert isinstance(es, _StubPGErrorStore)
        assert isinstance(ss, _StubPGSpecStore)
        assert noop_calls == [], "配置 postgresql 但误回退 no-op store"


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

    def test_pg_stores_implement_abc(self):
        from app.runtime.core.storage.pg_error_store import PGErrorStore
        from app.runtime.core.storage.pg_spec_store import PGSpecStore
        from app.runtime.core.storage.base import ErrorStorage, SpecStorage
        assert isinstance(PGErrorStore(), ErrorStorage)
        spec = PGSpecStore()
        assert isinstance(spec, SpecStorage)
        # 方法签名对齐 ABC
        for m in ("save_spec", "get_spec", "list_specs", "delete_spec"):
            assert callable(getattr(spec, m))

    def test_async_pg_stores_implement_abc(self):
        from app.runtime.core.storage.async_pg_store import AsyncPGErrorStore, AsyncPGSpecStore
        from app.runtime.core.storage.base import ErrorStorage, SpecStorage
        assert isinstance(AsyncPGErrorStore(), ErrorStorage)
        spec = AsyncPGSpecStore()
        assert isinstance(spec, SpecStorage)
        for m in ("save_spec", "get_spec", "list_specs", "delete_spec"):
            assert callable(getattr(spec, m))

    def test_noop_stores_implement_abc(self):
        from app.runtime.core.storage.noop_store import NoOpErrorStore, NoOpSpecStore
        from app.runtime.core.storage.base import ErrorStorage, SpecStorage
        assert isinstance(NoOpErrorStore(), ErrorStorage)
        assert isinstance(NoOpSpecStore(), SpecStorage)


# ════════════════════════════════════════════
#  asyncpg 异步存储测试（Phase 3.1）
#  需要 asyncpg 已安装（否则本节整体跳过）；用 fake pool/conn mock 测试关键方法
# ════════════════════════════════════════════

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg 未安装，跳过异步 PG 测试")


class _FakeRecord:
    """模拟 asyncpg.Record，支持 ['col'] 访问。"""

    def __init__(self, **kwargs):
        self._d = kwargs

    def __getitem__(self, key):
        return self._d[key]


class _FakeConn:
    """模拟 asyncpg 连接，记录 SQL 调用并返回预设结果。"""

    def __init__(self):
        self.calls = []  # [(method, sql, args)]
        self._fetch_queue = []      # list[list[_FakeRecord]]
        self._fetchrow_queue = []   # list[_FakeRecord | None]
        self._execute_status = "INSERT 0 1"

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self._execute_status

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._fetch_queue.pop(0) if self._fetch_queue else []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._fetchrow_queue.pop(0) if self._fetchrow_queue else None

    def transaction(self):
        """模拟 asyncpg conn.transaction() 异步上下文管理器（无真实事务语义）。"""
        return _FakeAcquireCtx(self)


class _FakeAcquireCtx:
    """模拟 asyncpg pool.acquire() 的 async context manager。"""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """模拟 asyncpg.Pool。"""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)

    async def close(self):
        pass


class TestAsyncPGStore:
    """用 mock asyncpg pool/conn 测试异步存储关键方法。

    不连接真实 PG：通过 fake pool/conn 拦截 execute/fetch/fetchrow 调用，
    断言生成的 SQL 与参数正确性，覆盖 save_entry/get_entries/upsert_error/
    save_spec/get_spec 及若干边界场景。
    """

    def setup_method(self):
        import app.runtime.core.storage.async_pg_store as mod
        self._mod = mod
        self._conn = _FakeConn()
        self._pool = _FakePool(self._conn)
        # 备份并替换模块级 _get_pool / _initialized，跳过 DDL 与真实建池
        self._orig_get_pool = mod._get_pool
        self._orig_initialized = mod._initialized
        mod._initialized = True

        async def _fake_get_pool():
            return self._pool

        mod._get_pool = _fake_get_pool

    def teardown_method(self):
        mod = self._mod
        mod._get_pool = self._orig_get_pool
        mod._initialized = self._orig_initialized

    def _execute_calls(self):
        """收集所有 execute 调用。"""
        return [c for c in self._conn.calls if c[0] == "execute"]

    @pytest.mark.asyncio
    async def test_save_entry(self):
        store = self._mod.AsyncPGTraceStore()
        await store.save_entry(
            "rid-1", {"timestamp": 1.0, "step": "start", "data": {"a": 1}}
        )
        execs = self._execute_calls()
        assert any("INSERT INTO traces" in c[1] for c in execs)
        insert = [c for c in execs if "INSERT INTO traces" in c[1]][0]
        args = insert[2]
        assert args[0] == "rid-1"
        assert args[2] == "start"
        assert json.loads(args[3]) == {"a": 1}  # data 序列化为 JSON 串

    @pytest.mark.asyncio
    async def test_save_entry_none_data(self):
        store = self._mod.AsyncPGTraceStore()
        await store.save_entry("rid-x", {"timestamp": 2.0, "step": "end", "data": None})
        execs = self._execute_calls()
        insert = [c for c in execs if "INSERT INTO traces" in c[1]][0]
        assert insert[2][3] is None  # data=None 传 NULL

    @pytest.mark.asyncio
    async def test_get_entries(self):
        self._conn._fetch_queue = [
            [
                _FakeRecord(timestamp=1.0, step="start", data='{"a": 1}'),
                _FakeRecord(timestamp=2.0, step="end", data=None),
            ]
        ]
        store = self._mod.AsyncPGTraceStore()
        entries = await store.get_entries("rid-1")
        assert len(entries) == 2
        assert entries[0]["step"] == "start"
        assert entries[0]["data"] == {"a": 1}
        assert entries[1]["data"] is None

    @pytest.mark.asyncio
    async def test_upsert_error(self):
        record = {
            "error_id": "e1", "fingerprint": "fp1", "type": "ValueError",
            "message": "boom", "frames": [{"file": "a.py"}], "frame_count": 1,
            "traceback": "tb", "source": "src", "session_id": "s1",
            "first_seen": 1.0, "last_seen": 2.0,
        }
        await self._mod.AsyncPGErrorStore().upsert_error(record)
        execs = self._execute_calls()
        assert any("INSERT INTO errors" in c[1] for c in execs)
        insert = [c for c in execs if "INSERT INTO errors" in c[1]][0]
        args = insert[2]
        assert args[0] == "e1"        # error_id
        assert args[1] == "fp1"       # fingerprint
        assert args[2] == "ValueError"
        assert args[8] == "s1"        # session_id
        assert json.loads(args[4]) == [{"file": "a.py"}]  # frames JSON

    @pytest.mark.asyncio
    async def test_upsert_error_default_session(self):
        """session_id 为 None 时写入 '_global'。"""
        await self._mod.AsyncPGErrorStore().upsert_error({
            "error_id": "e2", "fingerprint": "fp2", "type": "KeyError",
            "message": "miss", "session_id": None,
            "first_seen": 1.0, "last_seen": 2.0,
        })
        execs = self._execute_calls()
        insert = [c for c in execs if "INSERT INTO errors" in c[1]][0]
        assert insert[2][8] == "_global"

    @pytest.mark.asyncio
    async def test_save_spec(self):
        spec = {
            "id": "sp1", "kind": "api", "target": "/users",
            "expect": {"status": 200}, "created_at": 1.0, "updated_at": 2.0,
        }
        await self._mod.AsyncPGSpecStore().save_spec(spec)
        execs = self._execute_calls()
        assert any("INSERT INTO specs" in c[1] for c in execs)
        insert = [c for c in execs if "INSERT INTO specs" in c[1]][0]
        args = insert[2]
        assert args[0] == "sp1"
        assert args[1] == "api"
        assert args[2] == "/users"
        assert json.loads(args[3]) == {"status": 200}  # expect JSON

    @pytest.mark.asyncio
    async def test_get_spec(self):
        self._conn._fetchrow_queue = [
            _FakeRecord(
                id="sp1", kind="api", target="/users",
                expect='{"status": 200}', created_at=1.0, updated_at=2.0,
            )
        ]
        spec = await self._mod.AsyncPGSpecStore().get_spec("sp1")
        assert spec is not None
        assert spec["id"] == "sp1"
        assert spec["kind"] == "api"
        assert spec["target"] == "/users"
        assert spec["expect"] == {"status": 200}
        assert spec["created_at"] == 1.0
        assert spec["updated_at"] == 2.0

    @pytest.mark.asyncio
    async def test_get_spec_not_found(self):
        # fetchrow 队列为空 → 默认返回 None
        spec = await self._mod.AsyncPGSpecStore().get_spec("missing")
        assert spec is None

    @pytest.mark.asyncio
    async def test_delete_spec(self):
        self._conn._execute_status = "DELETE 1"
        ok = await self._mod.AsyncPGSpecStore().delete_spec("sp1")
        assert ok is True
        execs = self._execute_calls()
        assert any("DELETE FROM specs" in c[1] for c in execs)

    @pytest.mark.asyncio
    async def test_delete_spec_not_found(self):
        self._conn._execute_status = "DELETE 0"
        ok = await self._mod.AsyncPGSpecStore().delete_spec("missing")
        assert ok is False

    @pytest.mark.asyncio
    async def test_session_save_and_get(self):
        self._conn._fetchrow_queue = [
            _FakeRecord(
                session_id="s-1", created_at=1.0, last_active=2.0,
                metadata='{"role": "test"}',
            )
        ]
        store = self._mod.AsyncPGSessionStore()
        await store.save("s-1", {"session_id": "s-1", "created_at": 1.0, "metadata": {"role": "test"}})
        s = await store.get("s-1")
        assert s is not None
        assert s["session_id"] == "s-1"
        assert s["metadata"] == {"role": "test"}

    @pytest.mark.asyncio
    async def test_cleanup_expired_returns_affected_rows(self):
        self._conn._execute_status = "DELETE 3"
        store = self._mod.AsyncPGTraceStore()
        count = await store.cleanup_expired(ttl_seconds=3600)
        assert count == 3

    @pytest.mark.asyncio
    async def test_ensure_init_builds_kb_table(self, monkeypatch):
        """FIX b9-1: asyncpg _ensure_init 应建 kb_entries 表（此前漏建）。"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "pg_partition_enabled", False)
        monkeypatch.setattr(_settings, "pg_archive_enabled", False)

        self._mod._initialized = False
        await self._mod._ensure_init()

        execs = [c for c in self._conn.calls if c[0] == "execute"]
        assert any("kb_entries" in c[1] for c in execs), "asyncpg _ensure_init 应建 kb_entries 表"

    @pytest.mark.asyncio
    async def test_close_pool_resets_initialized(self, monkeypatch):
        """FIX b11-3: asyncpg close_pool 重置 _initialized（与同步 pg_executor 同口径）。"""
        monkeypatch.setattr(self._mod, "_pool", None)
        monkeypatch.setattr(self._mod, "_initialized", True)

        await self._mod.close_pool()

        assert self._mod._initialized is False


# ════════════════════════════════════════════
#  Phase 5 P3-1：分区工具函数测试（纯函数，无外部依赖）
# ════════════════════════════════════════════

class TestPartitionUtils:
    """分区相关工具函数的单元测试（纯函数，无需 DB）。"""

    def test_month_partition_name_format(self):
        """分区表名格式：traces_YYYY_MM，月份自动补零。"""
        from app.runtime.core.storage.pg_partitions import _month_partition_name
        assert _month_partition_name(2024, 1) == "traces_2024_01"
        assert _month_partition_name(2024, 12) == "traces_2024_12"
        assert _month_partition_name(2025, 6) == "traces_2025_06"

    def test_month_partition_name_async_same_format(self):
        """async 版本分区命名与同步版本一致。"""
        from app.runtime.core.storage.pg_partitions import _month_partition_name as sync_name
        from app.runtime.core.storage.async_pg_store import _month_partition_name as async_name
        assert sync_name(2024, 3) == async_name(2024, 3)
        assert sync_name(2025, 11) == async_name(2025, 11)

    def test_month_range_epoch_january(self):
        """1月分区范围：从 1月1日 00:00 到 2月1日 00:00。"""
        from app.runtime.core.storage.pg_partitions import _month_range_epoch
        from datetime import datetime, timezone

        start_ts, end_ts = _month_range_epoch(2024, 1)
        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)

        assert start_dt.year == 2024 and start_dt.month == 1 and start_dt.day == 1
        assert start_dt.hour == 0 and start_dt.minute == 0 and start_dt.second == 0
        assert end_dt.year == 2024 and end_dt.month == 2 and end_dt.day == 1
        assert end_ts > start_ts

    def test_month_range_epoch_december(self):
        """12月分区范围：跨年，到次年1月1日。"""
        from app.runtime.core.storage.pg_partitions import _month_range_epoch
        from datetime import datetime, timezone

        start_ts, end_ts = _month_range_epoch(2024, 12)
        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)

        assert start_dt.year == 2024 and start_dt.month == 12
        assert end_dt.year == 2025 and end_dt.month == 1

    def test_month_range_epoch_async_consistent(self):
        """sync 与 async 版本的月份范围计算结果一致。"""
        from app.runtime.core.storage.pg_partitions import _month_range_epoch as sync_range
        from app.runtime.core.storage.async_pg_store import _month_range_epoch as async_range

        for y, m in [(2024, 1), (2024, 6), (2024, 12), (2025, 3)]:
            s1, e1 = sync_range(y, m)
            s2, e2 = async_range(y, m)
            assert s1 == s2 and e1 == e2, f"不一致: {y}-{m}"

    def test_month_range_exclusive_upper_bound(self):
        """区间为 [start, end)，即 end 不属于本月。"""
        from app.runtime.core.storage.pg_partitions import _month_range_epoch
        start_ts, end_ts = _month_range_epoch(2024, 1)
        # 1月31日 23:59:59 属于本月
        from datetime import datetime, timezone
        jan_31_235959 = datetime(2024, 1, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        feb_1_000000 = datetime(2024, 2, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        assert start_ts <= jan_31_235959 < end_ts
        assert feb_1_000000 == end_ts


# ════════════════════════════════════════════
#  Phase 5 P3-1/P3-2：归档与分区 mock 集成测试
#  使用 fake conn 验证 SQL 正确性，无需真实 PG
# ════════════════════════════════════════════

class TestArchiveMock:
    """归档功能 mock 测试（基于 async fake conn，验证 SQL 逻辑）。"""

    def setup_method(self):
        import app.runtime.core.storage.async_pg_store as mod
        self._mod = mod
        self._conn = _FakeConn()
        self._pool = _FakePool(self._conn)
        self._orig_get_pool = mod._get_pool
        self._orig_initialized = mod._initialized
        mod._initialized = True

        async def _fake_get_pool():
            return self._pool

        mod._get_pool = _fake_get_pool

    def teardown_method(self):
        self._mod._get_pool = self._orig_get_pool
        self._mod._initialized = self._orig_initialized

    @pytest.mark.asyncio
    async def test_cleanup_expired_with_archive_enabled(self, monkeypatch):
        """启用归档时，cleanup_expired 先调用归档再删除。"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "pg_archive_enabled", True)
        monkeypatch.setattr(_settings, "pg_archive_days", 30)
        monkeypatch.setattr(_settings, "pg_archive_delete_after", True)

        self._conn._execute_status = "DELETE 5"
        store = self._mod.AsyncPGTraceStore()
        count = await store.cleanup_expired(ttl_seconds=3600)

        execs = [c for c in self._conn.calls if c[0] == "execute"]
        # 至少有一次归档相关调用（WITH moved AS ... INSERT INTO traces_archive）
        archive_calls = [c for c in execs if "traces_archive" in c[1]]
        assert len(archive_calls) >= 1, "启用归档后应调用归档 SQL"
        # 有 DELETE FROM traces
        delete_calls = [c for c in execs if "DELETE FROM traces" in c[1]]
        assert len(delete_calls) >= 1
        assert count == 5

    @pytest.mark.asyncio
    async def test_cleanup_expired_archive_disabled_no_archive_call(self, monkeypatch):
        """关闭归档时，cleanup_expired 不调用归档 SQL。"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "pg_archive_enabled", False)

        self._conn._execute_status = "DELETE 2"
        store = self._mod.AsyncPGTraceStore()
        count = await store.cleanup_expired(ttl_seconds=3600)

        execs = [c for c in self._conn.calls if c[0] == "execute"]
        archive_calls = [c for c in execs if "traces_archive" in c[1]]
        assert len(archive_calls) == 0, "关闭归档后不应调用归档 SQL"
        assert count == 2

    @pytest.mark.asyncio
    async def test_save_entry_with_partition_enabled_checks_partitions(self, monkeypatch):
        """启用分区时，save_entry 每 N 次写入后惰性检查分区。"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "pg_partition_enabled", True)
        monkeypatch.setattr(_settings, "pg_partition_precreate_months", 2)

        store = self._mod.AsyncPGTraceStore()
        store._write_counter = 999  # 手动设置到接近阈值

        # mock _ensure_partitions 函数
        ensure_calls = []

        async def _fake_ensure_partitions(conn):
            ensure_calls.append(True)
            return 0

        monkeypatch.setattr(self._mod, "_ensure_partitions", _fake_ensure_partitions)

        await store.save_entry("rid-1", {"timestamp": 1.0, "step": "s1", "data": None})
        # 第 1000 次写入，应该触发分区检查
        assert len(ensure_calls) == 1

    @pytest.mark.asyncio
    async def test_save_entry_partition_disabled_no_check(self, monkeypatch):
        """关闭分区时，save_entry 不检查分区。"""
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "pg_partition_enabled", False)

        store = self._mod.AsyncPGTraceStore()
        store._write_counter = 9999

        ensure_calls = []

        async def _fake_ensure_partitions(conn):
            ensure_calls.append(True)
            return 0

        monkeypatch.setattr(self._mod, "_ensure_partitions", _fake_ensure_partitions)

        await store.save_entry("rid-1", {"timestamp": 1.0, "step": "s1", "data": None})
        assert len(ensure_calls) == 0

    def test_should_check_partitions_every_1000_writes(self):
        """_should_check_partitions 每 1000 次返回 True 一次。"""
        store = self._mod.AsyncPGTraceStore()
        results = []
        for i in range(3001):
            results.append(store._should_check_partitions())
        # 第 1000、2000、3000 次返回 True
        assert results[999] is True
        assert results[1999] is True
        assert results[2999] is True
        # True 的总数应为 3
        assert sum(1 for r in results if r) == 3


# ════════════════════════════════════════════
#  批次6 Minor 修复回归（无需真实 PG，纯 mock / 纯函数）
# ════════════════════════════════════════════

class TestPGSessionStoreGuards:
    """FIX b6-3/b6-4：save 不突变输入 + metadata 非法 JSON 降级。"""

    def test_save_does_not_mutate_input_dict(self, monkeypatch):
        from app.runtime.core.storage import pg_session_store as mod
        from app.runtime.core.storage.pg_session_store import PGSessionStore

        class _FakeConn:
            closed = False

            def rollback(self):
                pass

        class _FakePool:
            def putconn(self, conn):
                pass

        captured = {}

        def _fake_execute(conn, sql, params=None, **kw):
            captured["params"] = params
            return conn, 1

        monkeypatch.setattr(mod, "_get_conn", lambda: _FakeConn())
        monkeypatch.setattr(mod, "_get_pool", lambda: _FakePool())
        monkeypatch.setattr(mod, "_execute_with_retry", _fake_execute)

        store = PGSessionStore.__new__(PGSessionStore)
        data = {"session_id": "s1", "created_at": 123.0, "metadata": {"role": "test"}}
        store.save("s1", data)

        # 输入 dict 未被突变（此前 data["last_active"] 会泄漏副作用）
        assert "last_active" not in data
        assert data == {"session_id": "s1", "created_at": 123.0, "metadata": {"role": "test"}}

    def test_safe_json_loads_valid_json(self):
        from app.runtime.core.storage.pg_session_store import _safe_json_loads
        assert _safe_json_loads('{"role": "test"}') == {"role": "test"}

    def test_safe_json_loads_invalid_json_degrades_to_empty_dict(self):
        from app.runtime.core.storage.pg_session_store import _safe_json_loads
        assert _safe_json_loads("{not valid json") == {}

    def test_safe_json_loads_non_string(self):
        from app.runtime.core.storage.pg_session_store import _safe_json_loads
        assert _safe_json_loads({"already": "dict"}) == {"already": "dict"}
        assert _safe_json_loads(None) == {}


class TestClosePoolResetsInitialized:
    """FIX b6-5：close_pool 重置 _initialized。"""

    def test_close_pool_resets_initialized(self, monkeypatch):
        import app.runtime.core.storage.pg_executor as mod

        class _FakePool:
            def closeall(self):
                pass

        monkeypatch.setattr(mod, "_pool", _FakePool())
        monkeypatch.setattr(mod, "_initialized", True)

        mod.close_pool()

        assert mod._initialized is False

    def test_close_pool_no_pool_still_resets(self, monkeypatch):
        import app.runtime.core.storage.pg_executor as mod

        monkeypatch.setattr(mod, "_pool", None)
        monkeypatch.setattr(mod, "_initialized", True)

        mod.close_pool()

        assert mod._initialized is False

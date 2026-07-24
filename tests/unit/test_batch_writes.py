"""单元测试：批量写入（P3-3）—— storage 批量接口 + logs 批量 API"""

from app.mcp.core.storage.memory_store import MemoryTraceStore
from app.mcp.core.storage.base import TraceStorage


class TestMemorySaveEntries:
    """MemoryTraceStore.save_entries 批量写入"""

    def test_save_entries_batch_writes_all(self):
        """批量写入多条条目，get_entries 返回全部"""
        store = MemoryTraceStore(max_entries=100)
        store.save_entries("req-1", [
            {"timestamp": 1.0, "step": "meta", "data": {"kind": "test"}},
            {"timestamp": 1.1, "step": "link", "data": {"caller": "abc"}},
            {"timestamp": 1.2, "step": "data", "data": {"type": "err"}},
        ])

        entries = store.get_entries("req-1")
        assert len(entries) == 3
        steps = [e["step"] for e in entries]
        assert steps == ["meta", "link", "data"]

    def test_save_entries_preserves_order(self):
        """批量写入保持条目顺序（SEC-13 语义）"""
        store = MemoryTraceStore(max_entries=100)
        store.save_entries("req-1", [
            {"timestamp": 1.0, "step": "third", "data": {}},
            {"timestamp": 1.0, "step": "first", "data": {}},
            {"timestamp": 1.0, "step": "second", "data": {}},
        ])

        entries = store.get_entries("req-1")
        steps = [e["step"] for e in entries]
        assert steps == ["third", "first", "second"]

    def test_save_entries_respects_capacity(self):
        """批量写入触发 FIFO 淘汰"""
        store = MemoryTraceStore(max_entries=2)
        store.save_entry("req-1", {"timestamp": 1.0, "step": "data", "data": {}})
        store.save_entry("req-2", {"timestamp": 2.0, "step": "data", "data": {}})

        store.save_entries("req-3", [
            {"timestamp": 3.0, "step": "data", "data": {}},
            {"timestamp": 3.1, "step": "data", "data": {}},
        ])

        assert "req-1" not in store._store
        assert "req-2" in store._store
        assert "req-3" in store._store

    def test_save_entries_empty_list(self):
        """空列表不报错"""
        store = MemoryTraceStore(max_entries=100)
        store.save_entries("req-1", [])
        entries = store.get_entries("req-1")
        assert entries == []


class TestBaseSaveEntriesDefault:
    """TraceStorage ABC 默认实现"""

    def test_default_save_entries_iterates(self):
        """ABC 默认实现逐条调用 save_entry"""
        class StubStorage(TraceStorage):
            def __init__(self):
                self.saved = []

            def save_entry(self, request_id, entry):
                self.saved.append((request_id, entry))

            def get_entries(self, request_id):
                return []

            def delete(self, request_id):
                pass

            def cleanup_expired(self, ttl_seconds):
                return 0

        store = StubStorage()
        store.save_entries("req-1", [
            {"step": "a", "data": 1},
            {"step": "b", "data": 2},
        ])

        assert len(store.saved) == 2
        assert store.saved[0] == ("req-1", {"step": "a", "data": 1})
        assert store.saved[1] == ("req-1", {"step": "b", "data": 2})


class TestLogsBatch:
    """logs.add_logs_batch 批量 API"""

    def test_add_logs_batch_writes_all(self):
        """批量写入多条日志"""
        from app.mcp.core.logs import add_logs_batch, get_logs

        add_logs_batch("test-batch", [
            ("meta", {"kind": "test"}),
            ("data", {"type": "err"}),
        ])

        entries = get_logs("test-batch")
        assert len(entries) == 2
        steps = [e["step"] for e in entries]
        assert steps == ["meta", "data"]

    def test_add_logs_batch_preserves_order(self):
        """批量写入保持顺序"""
        from app.mcp.core.logs import add_logs_batch, get_logs

        add_logs_batch("order-test", [
            ("c", {"x": 3}),
            ("a", {"x": 1}),
            ("b", {"x": 2}),
        ])

        entries = get_logs("order-test")
        steps = [e["step"] for e in entries]
        assert steps == ["c", "a", "b"]

    def test_add_logs_batch_empty_list(self):
        """空列表不报错"""
        from app.mcp.core.logs import add_logs_batch

        add_logs_batch("empty", [])

    def test_add_logs_batch_single_cache_invalidation(self, monkeypatch):
        """批量写入只触发一次 invalidate_cache"""
        call_count = [0]

        def mock_invalidate():
            call_count[0] += 1

        monkeypatch.setattr("app.api.dashboard.invalidate_cache", mock_invalidate)

        from app.mcp.core.logs import add_logs_batch

        add_logs_batch("cache-test", [
            ("meta", {}),
            ("link", {}),
            ("data", {}),
        ])

        assert call_count[0] == 1


class TestTraceRepoBatch:
    """trace_repo.save_trace 批量改造后的行为"""

    def test_save_trace_writes_meta_link_data_in_order(self):
        """save_trace 后 META/LINK/DATA 三条按顺序写入"""
        from app.mcp.core.trace_repo import save_trace
        from app.mcp.core.logs import get_logs

        error_id = save_trace(
            exc_type="RuntimeError",
            message="test error",
            frames=[],
            source="test",
            trace_kind="exception",
            trace_id="caller-trace-123",
        )

        entries = get_logs(error_id)
        assert len(entries) == 3
        steps = [e["step"] for e in entries]
        assert steps == ["trace_meta", "trace_link", "trace_data"]

    def test_save_trace_without_trace_id_skips_link(self):
        """不传 trace_id 时只有 META + DATA"""
        from app.mcp.core.trace_repo import save_trace
        from app.mcp.core.logs import get_logs

        error_id = save_trace(
            exc_type="RuntimeError",
            message="test error",
            frames=[],
            source="test",
            trace_kind="exception",
        )

        entries = get_logs(error_id)
        assert len(entries) == 2
        steps = [e["step"] for e in entries]
        assert steps == ["trace_meta", "trace_data"]

    def test_save_trace_data_is_last(self):
        """DATA 始终是最后一条（SEC-13 commit-marker 语义）"""
        from app.mcp.core.trace_repo import save_trace
        from app.mcp.core.logs import get_logs

        error_id = save_trace(
            exc_type="RuntimeError",
            message="test error",
            frames=[],
            source="test",
            trace_kind="exception",
            trace_id="caller-456",
        )

        entries = get_logs(error_id)
        last_entry = entries[-1]
        assert last_entry["step"] == "trace_data"

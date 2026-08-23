"""集成测试：PostgreSQL 存储、Dashboard、MCP Tools、LLM 端到端验证"""
import time
import uuid

import pytest

pytest.importorskip("psycopg2", reason="psycopg2 不可用，跳过 PG 集成测试")

from app.config import settings
from app.runtime.core.logs import add_log, get_logs, list_request_ids, delete_logs
from app.runtime.core.storage.factory import get_trace_store
from app.runtime.core.storage.pg_executor import _get_pool, _parse_data


@pytest.fixture(autouse=True)
def _require_pg():
    """仅在 STORAGE_BACKEND=postgresql 时执行"""
    if settings.storage_backend != "postgresql":
        pytest.skip("STORAGE_BACKEND != postgresql")


@pytest.fixture
def unique_request_id():
    rid = f"test-pg-{uuid.uuid4().hex[:12]}"
    yield rid
    delete_logs(rid)


@pytest.mark.integration
@pytest.mark.pg
class TestPGStoreConnection:
    """PostgreSQL 连接与基础操作"""

    def test_connection_pool_works(self):
        """连接池能正常获取/归还连接"""
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
            assert row[0] == 1
        finally:
            pool.putconn(conn)

    def test_table_auto_created(self):
        """traces 表存在"""
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = 'traces'"
            )
            assert cur.fetchone() is not None
        finally:
            pool.putconn(conn)

    def test_save_and_get_entry(self, unique_request_id):
        """save_entry + get_entries 往返一致"""
        store = get_trace_store()
        store.save_entry(unique_request_id, {
            "timestamp": time.time(),
            "step": "request_start",
            "data": {"method": "GET", "url": "/test"},
        })

        entries = store.get_entries(unique_request_id)
        assert len(entries) == 1
        assert entries[0]["step"] == "request_start"
        assert entries[0]["data"]["method"] == "GET"

    def test_save_string_data(self, unique_request_id):
        """data 为字符串时也能正常存储"""
        store = get_trace_store()
        store.save_entry(unique_request_id, {
            "timestamp": time.time(),
            "step": "error",
            "data": "InsufficientBalance: balance is -10",
        })

        entries = store.get_entries(unique_request_id)
        assert len(entries) == 1
        assert entries[0]["data"] == "InsufficientBalance: balance is -10"

    def test_list_request_ids(self, unique_request_id):
        """list_request_ids 能返回最近写入的 id"""
        store = get_trace_store()
        store.save_entry(unique_request_id, {
            "timestamp": time.time(),
            "step": "test",
            "data": {"ok": True},
        })

        ids = list_request_ids(limit=10)
        assert unique_request_id in ids

    def test_parse_data_helper(self):
        """_parse_data 正确处理各种类型"""
        assert _parse_data(None) is None
        assert _parse_data({"a": 1}) == {"a": 1}
        assert _parse_data([1, 2]) == [1, 2]
        assert _parse_data('{"key": "value"}') == {"key": "value"}
        assert _parse_data("plain string") == "plain string"
        assert _parse_data(42) == 42


@pytest.mark.integration
@pytest.mark.pg
class TestDashboardIntegration:
    """Dashboard API 从 PostgreSQL 读取"""

    def test_stats_returns_valid_structure(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.dashboard import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "total_traces" in body
        assert "silent_failures" in body
        assert "exceptions" in body
        assert "spec_count" in body

    def test_traces_list_from_pg(self, unique_request_id):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.dashboard import router

        add_log(unique_request_id, "request_start", {"method": "POST", "url": "/api/test"})
        add_log(unique_request_id, "response_ready", {"status": 200})

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/dashboard/traces?limit=50")
        assert resp.status_code == 200
        body = resp.json()
        trace_ids = [t["trace_id"] for t in body["traces"]]
        assert unique_request_id in trace_ids

    def test_trace_detail_from_pg(self, unique_request_id):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.dashboard import router

        add_log(unique_request_id, "request_start", {"method": "GET", "url": "/detail"})
        add_log(unique_request_id, "response_ready", {"status": 200})

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get(f"/api/dashboard/trace/{unique_request_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == unique_request_id


@pytest.mark.integration
@pytest.mark.pg
class TestMCPToolIntegration:
    """MCP Tools 从 PostgreSQL 读取"""

    def test_list_recent_traces_includes_pg_data(self, unique_request_id):
        from app.mcp.tools.trace_api import list_recent_traces

        add_log(unique_request_id, "request_start", {"method": "GET", "url": "/mcp-test"})
        add_log(unique_request_id, "response_ready", {"status": 200})

        traces = list_recent_traces(limit=20)
        trace_ids = [t["trace_id"] for t in traces]
        assert unique_request_id in trace_ids

    def test_search_logs_finds_pg_data(self, unique_request_id):
        from app.mcp.tools.trace_api import search_logs

        keyword = f"unique-kw-{uuid.uuid4().hex[:8]}"
        add_log(unique_request_id, "request_start", {"method": "GET", "url": f"/{keyword}"})

        results = search_logs(keyword, since_minutes=60)
        trace_ids = [r["trace_id"] for r in results]
        assert unique_request_id in trace_ids

    def test_get_logs_returns_pg_data(self, unique_request_id):
        add_log(unique_request_id, "step1", {"value": 1})
        add_log(unique_request_id, "step2", {"value": 2})

        entries = get_logs(unique_request_id)
        assert len(entries) == 2
        steps = [e["step"] for e in entries]
        assert "step1" in steps
        assert "step2" in steps


@pytest.mark.integration
@pytest.mark.llm
class TestLLMIntegration:
    """LLM 分析端到端（仅在配置了 LLM 时执行）"""

    def test_analyze_with_llm_returns_structure(self, unique_request_id):
        """LLM 端到端分析。三状态显式处理：
        1) 未配置（OPENAI_API_KEY 为空）→ 显式 skip
        2) 配置但调用失败（网络/超时/API 错误）→ 真实抛出 fail
        3) 配置且成功 → 断言返回结构

        不再用 try/except 吞断言，避免失败被静默降级为 skip。
        """
        if not settings.openai_api_key:
            pytest.skip(
                "LLM 未配置（OPENAI_API_KEY 为空），跳过端到端 LLM 测试。"
                "如需启用，请在 .env 配置 API Key"
            )

        from app.mcp.tools.debug_api import analyze_with_llm

        add_log(unique_request_id, "request_start", {"method": "GET", "url": "/llm-test"})
        add_log(unique_request_id, "error", {
            "error_type": "ConnectionError",
            "message": "Connection refused",
        })

        # 不再 try/except 吞断言：调用失败和断言失败都应真实抛出
        result = analyze_with_llm(unique_request_id)
        assert "analysis" in result or "error" in result


@pytest.mark.integration
@pytest.mark.pg
class TestTraceRepoPersistence:
    """C3/C4：trace_repo 写入 → 重启内存清空 → get_trace 从 PG 读回"""

    def test_save_trace_persists_to_pg(self, unique_request_id):
        """C4 上半段：save_trace 后 PG 中能查到 trace_data / trace_meta 条目"""
        from app.runtime.core import trace_repo

        error_id = trace_repo.save_trace(
            "ValueError", "bad value",
            [{"file": "a.py", "line": 10, "function": "f"}],
            source="ingest", extra={"context": "pg-test"},
            trace_kind="exception",
        )
        try:
            # PG 中应能查到 step=trace_data / step=trace_meta
            entries = get_logs(error_id)
            steps = [e.get("step") for e in entries]
            assert "trace_data" in steps
            assert "trace_meta" in steps

            data_entries = [e for e in entries if e.get("step") == "trace_data"]
            assert len(data_entries) == 1
            data = data_entries[0].get("data") or {}
            assert data["type"] == "ValueError"
            assert data["message"] == "bad value"
            assert data["source"] == "ingest"

            meta_entries = [e for e in entries if e.get("step") == "trace_meta"]
            assert len(meta_entries) == 1
            meta = meta_entries[0].get("data") or {}
            assert meta["trace_kind"] == "exception"
            assert meta["extra"] == {"context": "pg-test"}

            # error_id 必须在 list_request_ids 中
            assert error_id in list_request_ids(limit=200)
        finally:
            # 清理 trace_repo 写入的 trace_store 数据
            delete_logs(error_id)

    def test_get_trace_reads_back_from_pg_after_errors_clear(self, unique_request_id):
        """C4 下半段：写入 → 清空 errors 内存 → get_trace 仍能从 PG 读回"""
        from app.runtime.core import trace_repo
        from app.runtime.core import errors

        error_id = trace_repo.save_trace(
            "SilentFailure", "click no response",
            [{"file": "btn.tsx", "line": 42, "function": "onClick"}],
            source="browser_sdk",
            extra={"expectation": "route_change"},
            trace_kind="silent_failure",
        )
        try:
            # 1. 正常场景：errors 内存命中
            got = trace_repo.get_trace(error_id)
            assert got is not None
            assert got["trace_id"] == error_id
            assert got["exc_type"] == "SilentFailure"
            assert got["trace_kind"] == "silent_failure"

            # 2. 模拟重启：清空 errors 内存缓冲，PG 保留
            errors._recent.clear()

            # 3. get_trace 应能从 PG 回读重建
            got2 = trace_repo.get_trace(error_id)
            assert got2 is not None, "重启 errors 内存清空后 get_trace 应从 PG 回读"
            assert got2["trace_id"] == error_id
            assert got2["exc_type"] == "SilentFailure"
            assert got2["message"] == "click no response"
            assert got2["frames"] == [{"file": "btn.tsx", "line": 42, "function": "onClick"}]
            assert got2["frame_count"] == 1
            assert got2["trace_kind"] == "silent_failure"
            assert got2["extra"] == {"expectation": "route_change"}
            assert got2.get("from_store") is True
        finally:
            delete_logs(error_id)

    def test_save_trace_with_caller_trace_id_keys_unified_in_pg(self, unique_request_id):
        """C3 SDK 场景：传入 trace_id，PG 中 add_log key 必须统一为 error_id"""
        from app.runtime.core import trace_repo

        caller_tid = f"sdk-{uuid.uuid4().hex[:8]}"
        error_id = trace_repo.save_trace(
            "Error", "msg", [],
            source="browser_sdk", trace_id=caller_tid,
        )
        try:
            # error_id 在 PG list_request_ids 中
            assert error_id in list_request_ids(limit=200)
            # caller_trace_id 不应作为 add_log key 出现在 PG 中
            assert caller_tid not in list_request_ids(limit=200)

            # error_id 下应有 trace_data / trace_meta / trace_link 三条
            entries = get_logs(error_id)
            steps = [e.get("step") for e in entries]
            assert "trace_data" in steps
            assert "trace_meta" in steps
            assert "trace_link" in steps

            # trace_link 记录 caller_trace_id
            link_entries = [e for e in entries if e.get("step") == "trace_link"]
            assert len(link_entries) == 1
            link_data = link_entries[0].get("data") or {}
            assert link_data.get("caller_trace_id") == caller_tid
        finally:
            delete_logs(error_id)


@pytest.mark.integration
@pytest.mark.pg
class TestKnowledgeBasePersistence:
    """v0.5.3 kb_entries 表：KB 写穿持久化 + 启动回灌（真实 PG 往返）"""

    def _cleanup(self, fingerprint: str):
        from app.runtime.core.storage.factory import get_knowledge_store

        get_knowledge_store().delete_kb_entry(fingerprint)

    def test_kb_entry_roundtrip(self):
        """upsert 写穿 → 清内存 → 回灌：analysis/验证统计跨"重启"保留"""
        from app.rag.knowledge_base import KnowledgeBaseStore
        from app.runtime.core.storage.factory import get_knowledge_store

        fp = f"itest-kb-{uuid.uuid4().hex[:12]}"
        store = KnowledgeBaseStore(max_entries=10)
        try:
            entry = store.upsert(
                fingerprint=fp,
                analysis={"root_cause": "db timeout", "exception_type": "OperationalError"},
                fix_suggestion="add retry with backoff",
                source="itest",
            )
            store.record_verification(fp, 0.88)

            # 持久层已有该行（写穿生效）
            pg = get_knowledge_store()
            rows = [r for r in pg.list_recent_kb_entries(limit=100) if r["fingerprint"] == fp]
            assert len(rows) == 1
            assert rows[0]["analysis"]["root_cause"] == "db timeout"
            assert rows[0]["verify_count"] == 1
            assert rows[0]["case_confidence"] == 0.88
            assert rows[0]["normalized_fingerprint"] == entry["normalized_fingerprint"]

            # 模拟重启：全新内存实例回灌
            store2 = KnowledgeBaseStore(max_entries=10)
            loaded = store2.load_from_persistent()
            assert loaded > 0
            restored = store2.get(fp)
            assert restored is not None
            assert restored["analysis"]["root_cause"] == "db timeout"
            assert restored["fix_suggestion"] == "add retry with backoff"
            assert restored["verify_count"] == 1
            assert restored["case_confidence"] == 0.88

            # 回灌后可继续写穿（update 路径）
            store2.record_verification(fp, 0.95)
            rows2 = [r for r in pg.list_recent_kb_entries(limit=100) if r["fingerprint"] == fp]
            assert rows2[0]["verify_count"] == 2
        finally:
            self._cleanup(fp)

    def test_kb_entry_eviction_deletes_row(self):
        """LRU 驱逐同步删除持久行：内存与 PG 条数一致"""
        from app.rag.knowledge_base import KnowledgeBaseStore
        from app.runtime.core.storage.factory import get_knowledge_store

        base = f"itest-kbev-{uuid.uuid4().hex[:8]}"
        store = KnowledgeBaseStore(max_entries=2)
        try:
            store.upsert(fingerprint=f"{base}-1", analysis={"a": 1}, fix_suggestion="", source="itest")
            store.upsert(fingerprint=f"{base}-2", analysis={"a": 2}, fix_suggestion="", source="itest")
            store.upsert(fingerprint=f"{base}-3", analysis={"a": 3}, fix_suggestion="", source="itest")

            pg = get_knowledge_store()
            remaining = [
                r["fingerprint"]
                for r in pg.list_recent_kb_entries(limit=100)
                if r["fingerprint"].startswith(base)
            ]
            # 断言顺序无关但严格：update_at 均为 DOUBLE PRECISION epoch 秒，
            # 连续写入的两条可能在精度内同值，ORDER BY updated_at DESC 的返回顺序
            # 不属本用例契约（本用例目标是验证驱逐删除第 1 行、保留第 2/3 行）。
            # 先验数量再验集合，避免仅 set 比较掩盖重复/额外记录。
            assert len(remaining) == 2
            assert set(remaining) == {f"{base}-2", f"{base}-3"}
            assert store.size() == 2
        finally:
            for i in (1, 2, 3):
                self._cleanup(f"{base}-{i}")

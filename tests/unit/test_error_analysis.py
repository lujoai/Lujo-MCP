"""智能错误分析引擎单元测试（Phase 7）"""

import pytest
import time
from unittest.mock import patch, MagicMock

from app.runtime.core import errors
from app.api import dashboard as dashboard_module


@pytest.fixture(autouse=True)
def _clear_cache():
    dashboard_module._cache.clear()


def _frames_a():
    return [{"file": "a.py", "line": 10, "function": "func_a"}]


def _frames_b():
    return [{"file": "b.py", "line": 20, "function": "func_b"}]


class TestAggregateByFingerprint:
    """aggregate_by_fingerprint 测试"""

    def test_aggregates_same_fingerprint(self):
        """相同指纹的错误被聚合"""
        errors.record({"type": "ValueError", "message": "err1", "frames": _frames_a()}, session_id="sess-1")
        errors.record({"type": "ValueError", "message": "err2", "frames": _frames_a()}, session_id="sess-1")

        aggregates = errors.aggregate_by_fingerprint()
        assert len(aggregates) == 1
        assert aggregates[0]["total_occurrences"] == 2
        assert aggregates[0]["message"] == "err2"
        assert len(aggregates[0]["error_ids"]) == 1

    def test_separates_different_fingerprints(self):
        """不同指纹的错误分开聚合"""
        errors.record({"type": "ValueError", "message": "err1", "frames": _frames_a()}, session_id="sess-1")
        errors.record({"type": "TypeError", "message": "err2", "frames": _frames_b()}, session_id="sess-1")

        aggregates = errors.aggregate_by_fingerprint()
        assert len(aggregates) == 2
        assert set(g["type"] for g in aggregates) == {"ValueError", "TypeError"}

    def test_counts_affected_sessions(self):
        """统计影响的 session 数量"""
        errors.record({"type": "ValueError", "message": "err", "frames": _frames_a()}, session_id="sess-1")
        errors.record({"type": "ValueError", "message": "err", "frames": _frames_a()}, session_id="sess-2")

        aggregates = errors.aggregate_by_fingerprint()
        assert len(aggregates) == 1
        assert aggregates[0]["affected_sessions"] == 2

    def test_filters_by_session_id(self):
        """按 session_id 过滤"""
        errors.record({"type": "ValueError", "message": "err", "frames": _frames_a()}, session_id="sess-1")
        errors.record({"type": "TypeError", "message": "err", "frames": _frames_b()}, session_id="sess-2")

        result = errors.aggregate_by_fingerprint(session_id="sess-1")
        assert len(result) == 1
        assert result[0]["type"] == "ValueError"

    def test_samples_limit_to_three(self):
        """samples 最多保留 3 条"""
        for i in range(5):
            errors.record({"type": "ValueError", "message": f"err{i}", "frames": _frames_a()}, session_id=f"sess-{i}")

        aggregates = errors.aggregate_by_fingerprint()
        assert len(aggregates) == 1
        assert len(aggregates[0]["samples"]) == 3

    def test_error_ids_limit_to_ten(self):
        """error_ids 最多保留 10 条"""
        for i in range(15):
            errors.record({"type": "ValueError", "message": f"err{i}", "frames": _frames_a()}, session_id=f"sess-{i}")

        aggregates = errors.aggregate_by_fingerprint()
        assert len(aggregates) == 1
        assert len(aggregates[0]["error_ids"]) == 10


class TestRankByImpact:
    """rank_by_impact 测试"""

    def test_ranks_by_impact_score(self):
        """按影响分数排序"""
        errors.record({"type": "ValueError", "message": "high", "frames": _frames_a()}, session_id="sess-1")
        errors.record({"type": "ValueError", "message": "high", "frames": _frames_a()}, session_id="sess-1")
        errors.record({"type": "TypeError", "message": "low", "frames": _frames_b()}, session_id="sess-1")

        ranked = errors.rank_by_impact()
        assert len(ranked) == 2
        assert ranked[0]["type"] == "ValueError"
        assert ranked[0]["impact_score"] >= ranked[1]["impact_score"]

    def test_has_impact_score(self):
        """包含 impact_score 字段"""
        errors.record({"type": "ValueError", "message": "test", "frames": _frames_a()}, session_id="sess-1")

        ranked = errors.rank_by_impact()
        assert len(ranked) == 1
        assert "impact_score" in ranked[0]
        assert isinstance(ranked[0]["impact_score"], (int, float))

    def test_has_hours_since_last_seen(self):
        """包含 hours_since_last_seen 字段"""
        errors.record({"type": "ValueError", "message": "test", "frames": _frames_a()}, session_id="sess-1")

        ranked = errors.rank_by_impact()
        assert len(ranked) == 1
        assert "hours_since_last_seen" in ranked[0]

    def test_filters_by_time_window(self):
        """按时间窗口过滤"""
        errors.record({"type": "ValueError", "message": "old", "frames": _frames_a()}, session_id="sess-1")
        time.sleep(0.1)

        ranked = errors.rank_by_impact(since_minutes=0)
        assert len(ranked) == 0

    def test_empty_when_no_errors(self):
        """无错误时返回空列表"""
        ranked = errors.rank_by_impact()
        assert ranked == []


class TestQueryPgErrors:
    """query_pg_errors 测试"""

    def test_returns_empty_when_not_postgresql(self):
        """非 PostgreSQL 后端返回空列表"""
        with patch("app.config.settings") as mock_settings:
            mock_settings.storage_backend = "memory"
            result = errors.query_pg_errors()
            assert result == []

    def test_returns_empty_on_pg_error(self):
        """PG 连接失败时返回空列表"""
        with patch("app.config.settings") as mock_settings:
            mock_settings.storage_backend = "postgresql"
            with patch("app.runtime.core.storage.pg_store._get_pool") as mock_pool:
                mock_pool.side_effect = RuntimeError("connection failed")
                result = errors.query_pg_errors()
                assert result == []

    def test_filters_by_fingerprint(self):
        """按指纹过滤（mock）"""
        mock_row = (
            "err-123", "fp-abc", "ValueError", "msg", None, 0, None, "test", "_global",
            1, time.time(), time.time(), None, None,
        )
        with patch("app.config.settings") as mock_settings:
            mock_settings.storage_backend = "postgresql"
            with patch("app.runtime.core.storage.pg_store._get_pool") as mock_pool:
                mock_conn = MagicMock()
                mock_cur = MagicMock()
                mock_pool.return_value.getconn.return_value = mock_conn
                mock_conn.cursor.return_value = mock_cur
                mock_cur.fetchall.return_value = [mock_row]

                result = errors.query_pg_errors(fingerprint="fp-abc")
                assert len(result) == 1
                assert result[0]["fingerprint"] == "fp-abc"


class TestDashboardErrorsEndpoints:
    """Dashboard 错误分析端点测试"""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.dashboard import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_aggregated_endpoint(self, client):
        """aggregated 端点返回正确结构"""
        errors.record({"type": "ValueError", "message": "test", "frames": _frames_a()}, session_id="sess-1")

        resp = client.get("/api/dashboard/errors/aggregated")
        assert resp.status_code == 200
        body = resp.json()
        assert "aggregates" in body
        assert "total_fingerprints" in body
        assert "total_occurrences" in body
        assert body["total_fingerprints"] == 1
        assert body["total_occurrences"] == 1

    def test_ranked_endpoint(self, client):
        """ranked 端点返回正确结构"""
        errors.record({"type": "ValueError", "message": "test", "frames": _frames_a()}, session_id="sess-1")

        resp = client.get("/api/dashboard/errors/ranked")
        assert resp.status_code == 200
        body = resp.json()
        assert "ranked_errors" in body
        assert "total" in body
        assert "since_minutes" in body
        assert body["total"] == 1

    def test_ranked_endpoint_params(self, client):
        """ranked 端点参数限制"""
        resp = client.get("/api/dashboard/errors/ranked?since_minutes=0&limit=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["since_minutes"] == 1
        assert body["total"] == 0

    def test_history_endpoint(self, client):
        """history 端点返回正确结构"""
        resp = client.get("/api/dashboard/errors/history")
        assert resp.status_code == 200
        body = resp.json()
        assert "errors" in body
        assert "total" in body
        assert "since_minutes" in body

    def test_history_endpoint_params(self, client):
        """history 端点参数限制"""
        resp = client.get("/api/dashboard/errors/history?since_minutes=0&limit=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["since_minutes"] == 1
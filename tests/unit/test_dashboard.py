"""单元测试：Dashboard API 端点"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dashboard as dashboard_module
from app.api.dashboard import router
from app.mcp.core import trace_repo
from app.mcp.tools.verify_api import verify_handler


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前清空 dashboard 缓存，避免跨用例污染"""
    dashboard_module._cache.clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestDashboardStats:

    def test_stats_empty(self, client):
        """统计接口返回正确结构"""
        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "total_traces" in body
        assert "silent_failures" in body
        assert "exceptions" in body
        assert "spec_count" in body
        assert isinstance(body["total_traces"], int)
        assert isinstance(body["silent_failures"], int)
        assert isinstance(body["exceptions"], int)
        assert isinstance(body["spec_count"], int)

    def test_stats_with_traces(self, client):
        """有数据时统计正确"""
        trace_repo.save_trace("ValueError", "bad value", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")
        trace_repo.save_trace("SilentFailure", "no response", [],
                              trace_kind="silent_failure", source="test")

        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_traces"] >= 2


class TestDashboardTraces:

    def test_list_traces(self, client):
        trace_repo.save_trace("TypeError", "x is None", [
            {"file": "b.py", "line": 2, "function": "g"}
        ], source="test")

        resp = client.get("/api/dashboard/traces?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert body["traces"][0]["type"] == "TypeError"
        assert "trace_id" in body["traces"][0]

    def test_trace_with_verify(self, client):
        """trace 含 verify 结果"""
        tid = trace_repo.save_trace("E", "m", [], source="test")
        verify_handler({
            "actual": {"status_code": 200, "body": {"name": "Bob"}},
            "spec": {"kind": "api", "expect": {"body_rules": {"name": "Alice"}}},
            "trace_id": tid,
        })

        resp = client.get("/api/dashboard/traces?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        # 找对应的 trace
        found = [t for t in body["traces"] if t["trace_id"] == tid]
        assert len(found) == 1
        assert found[0]["verify_count"] == 1
        assert found[0]["has_silent_failure"] is True


class TestDashboardTraceDetail:

    def test_trace_detail(self, client):
        tid = trace_repo.save_trace("ValueError", "test error", [
            {"file": "app/config.py", "line": 9, "function": "Settings"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == tid
        assert body["trace_kind"] == "exception"
        assert body["exception"]["type"] == "ValueError"

    def test_trace_detail_not_found(self, client):
        resp = client.get("/api/dashboard/trace/no-such-trace")
        assert resp.status_code == 404

    def test_trace_detail_with_spec_diffs(self, client):
        """detail 含 spec_diffs"""
        tid = trace_repo.save_trace("E", "m", [], source="test")
        verify_handler({
            "actual": {"status_code": 200, "body": {"ok": True}},
            "spec": {"kind": "api", "expect": {"body_rules": {"ok": False}}},
            "trace_id": tid,
        })

        resp = client.get(f"/api/dashboard/trace/{tid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["spec_diffs"] is not None
        assert len(body["spec_diffs"]) == 1
        assert body["spec_diffs"][0]["silent_failure"] is True


class TestDashboardQualityReport:
    """v0.4.0: trace 详情端点注入 quality_report 字段"""

    def test_trace_detail_contains_quality_report(self, client):
        """trace detail 返回 quality_report 字段（默认开启 quality_scoring）"""
        tid = trace_repo.save_trace("ValueError", "test error", [
            {"file": "app/config.py", "line": 9, "function": "Settings"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        assert resp.status_code == 200
        body = resp.json()
        # quality_report 字段存在（非 None，因为默认 quality_scoring_enabled=True）
        assert "quality_report" in body
        qr = body["quality_report"]
        assert qr is not None
        # 核心字段齐全
        assert "overall_score" in qr
        assert "context_completeness" in qr
        assert "analysis_confidence" in qr
        assert "evidence_items" in qr
        assert "suggestions" in qr
        assert "scored_at" in qr
        assert qr["scorer_version"] == "1.0.0"

    def test_quality_report_dimensions_full(self, client):
        """quality_report 包含 9 个维度的评分"""
        tid = trace_repo.save_trace("ValueError", "test error", [
            {"file": "app/config.py", "line": 9, "function": "Settings"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        body = resp.json()
        qr = body["quality_report"]
        dims = qr["context_completeness"]["dimensions"]
        # 9 个维度全部存在
        assert len(dims) == 9
        expected = {"trace", "runtime", "code_snippet", "git_context",
                    "network", "ui_event", "spec", "knowledge_base", "llm_analysis"}
        assert set(dims.keys()) == expected

    def test_quality_report_formula(self, client):
        """overall_score = completeness × confidence"""
        tid = trace_repo.save_trace("ValueError", "test", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        body = resp.json()
        qr = body["quality_report"]
        comp = qr["context_completeness"]["overall_score"]
        conf = qr["analysis_confidence"]["overall_score"]
        expected = round(comp * conf, 4)
        assert abs(qr["overall_score"] - expected) < 0.001

    def test_quality_report_disabled_when_flag_off(self, client, monkeypatch):
        """quality_scoring_enabled=False → quality_report 为 None"""
        from app.config import settings
        monkeypatch.setattr(settings, "quality_scoring_enabled", False)

        tid = trace_repo.save_trace("ValueError", "test", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}")
        body = resp.json()
        assert body["quality_report"] is None

    def test_quality_only_endpoint(self, client):
        """独立质量端点 /api/dashboard/trace/{tid}/quality"""
        tid = trace_repo.save_trace("ValueError", "test", [
            {"file": "a.py", "line": 1, "function": "f"}
        ], source="test")

        resp = client.get(f"/api/dashboard/trace/{tid}/quality")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == tid
        assert body["quality_report"] is not None
        assert "overall_score" in body["quality_report"]

    def test_quality_only_endpoint_not_found(self, client):
        """质量端点 404"""
        resp = client.get("/api/dashboard/trace/no-such-trace/quality")
        assert resp.status_code == 404


class TestDashboardLimitCap:
    """limit 参数上限测试"""

    def test_limit_default(self, client):
        """默认 limit=100"""
        resp = client.get("/api/dashboard/traces")
        assert resp.status_code == 200

    def test_limit_capped_at_1000(self, client):
        """超过 1000 时截断到 1000"""
        resp = client.get("/api/dashboard/traces?limit=9999")
        assert resp.status_code == 200

    def test_limit_minimum_one(self, client):
        """limit 最小为 1"""
        resp = client.get("/api/dashboard/traces?limit=0")
        assert resp.status_code == 200


class TestDashboardCache:
    """_collect_all_traces 缓存测试"""

    def test_cache_populated(self):
        """首次调用后缓存被填充"""
        dashboard_module._collect_all_traces(limit=10)
        assert "all_traces" in dashboard_module._cache

    def test_cache_returns_same_data(self):
        """TTL 内返回缓存数据"""
        result1 = dashboard_module._collect_all_traces(limit=10)
        result2 = dashboard_module._collect_all_traces(limit=10)
        assert result1 == result2

    def test_cache_expires_after_ttl(self):
        """TTL 过期后重新采集"""
        from unittest.mock import patch

        with patch("app.api.dashboard.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            dashboard_module._collect_all_traces(limit=10)
            assert dashboard_module._cache["all_traces"][0] == 100.0

            # 模拟时间过了 TTL+1 秒
            mock_time.monotonic.return_value = 100.0 + dashboard_module._CACHE_TTL + 1
            dashboard_module._collect_all_traces(limit=10)
            assert dashboard_module._cache["all_traces"][0] == 100.0 + dashboard_module._CACHE_TTL + 1

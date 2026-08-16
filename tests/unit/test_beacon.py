"""单元测试：beacon 短时令牌（CODE_REVIEW S1）—— 签发/校验/作用域/过期 + 中间件鉴权流程。"""

import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import beacon
from app.auth.rbac import require_role
from app.config import settings


@pytest.fixture(autouse=True)
def _clean_mem():
    """每个用例前清空 beacon 内存存储，避免状态污染。"""
    with beacon._lock:
        beacon._mem.clear()
    yield
    with beacon._lock:
        beacon._mem.clear()


class TestBeaconIssueVerify:
    def test_issue_and_verify_within_scope(self, monkeypatch):
        monkeypatch.setattr(settings, "beacon_token_ttl_seconds", 60)
        token = beacon.issue_beacon_token(role="admin")
        # 默认 scope 覆盖 /ingest 与 /api/dashboard/stream
        assert beacon.verify_beacon_token(token, "/ingest/batch") == "admin"
        assert beacon.verify_beacon_token(token, "/api/dashboard/stream") == "admin"

    def test_out_of_scope_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "beacon_token_scope", "/ingest")
        token = beacon.issue_beacon_token(role="developer")
        assert beacon.verify_beacon_token(token, "/ingest/batch") == "developer"
        # 作用域外 → fail-closed
        assert beacon.verify_beacon_token(token, "/api/dashboard/stream") is None

    def test_wrong_or_empty_token_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "beacon_token_ttl_seconds", 60)
        token = beacon.issue_beacon_token(role="admin")
        assert beacon.verify_beacon_token(token + "x", "/ingest/batch") is None
        assert beacon.verify_beacon_token("", "/ingest/batch") is None

    def test_expired_token_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "beacon_token_ttl_seconds", 60)
        token = beacon.issue_beacon_token(role="admin")
        # 直接篡改内存中的过期时间，模拟 TTL 已到
        key = "beacon:" + beacon._digest(token)
        with beacon._lock:
            beacon._mem[key]["expires_at"] = time.time() - 1
        assert beacon.verify_beacon_token(token, "/ingest/batch") is None

    def test_issue_custom_scope(self, monkeypatch):
        monkeypatch.setattr(settings, "beacon_token_scope", "/ingest,/api/dashboard/stream")
        token = beacon.issue_beacon_token(role="viewer", scope="/ingest")
        assert beacon.verify_beacon_token(token, "/ingest/error") == "viewer"
        # 自定义 scope 未包含 dashboard → 拒绝
        assert beacon.verify_beacon_token(token, "/api/dashboard/stream") is None

    def test_prefix_boundary_not_bypassed(self, monkeypatch):
        """scope 前缀匹配须有边界：/ingest 不得放行 /ingest-malicious 等相似端点。

        复现 CODE_REVIEW P3-4：``path.startswith(scope)`` 无边界，
        ``/ingest-xxx`` / ``/ingestion`` / ``/ingestfoo`` 会被误判为作用域内。
        """
        monkeypatch.setattr(settings, "beacon_token_scope", "/ingest")
        token = beacon.issue_beacon_token(role="viewer")
        # 真实作用域内：scope 本身 + 子路径
        assert beacon.verify_beacon_token(token, "/ingest") == "viewer"
        assert beacon.verify_beacon_token(token, "/ingest/batch") == "viewer"
        # 前缀相似但属于不同端点 → 必须 fail-closed
        assert beacon.verify_beacon_token(token, "/ingest-malicious") is None
        assert beacon.verify_beacon_token(token, "/ingestion") is None
        assert beacon.verify_beacon_token(token, "/ingestfoo") is None


class TestBeaconMiddlewareFlow:
    """中间件：header 换取令牌后，URL 只带 ?token= 即可通过（作用域内）。"""

    def _build_app(self, monkeypatch):
        import app.middleware as mw

        monkeypatch.setattr(settings, "api_keys", "k1")
        monkeypatch.setattr(settings, "api_key", None)
        monkeypatch.setattr(settings, "rbac_enabled", True)
        monkeypatch.setattr(settings, "rbac_role_mapping", "k1:admin")

        app = FastAPI()

        @app.post("/ingest/error", dependencies=[Depends(require_role("admin", "developer"))])
        def _ingest():
            return {"ok": True}

        app.add_middleware(mw.AuthMiddleware)
        return TestClient(app)

    def test_token_in_query_authenticates_ingest(self, monkeypatch):
        client = self._build_app(monkeypatch)
        token = beacon.issue_beacon_token(role="admin")
        # URL 只带短时令牌 → 放行（不再需要永久 Key）
        assert client.post(f"/ingest/error?token={token}").status_code == 200
        # 无令牌 → 401
        assert client.post("/ingest/error").status_code == 401

    def test_permanent_key_in_query_now_rejected(self, monkeypatch):
        """?api_key= 永久 Key 查询参数降级已被移除（S1）→ 401。"""
        client = self._build_app(monkeypatch)
        assert client.post("/ingest/error?api_key=k1").status_code == 401
        # 但 header 仍可用
        assert client.post("/ingest/error", headers={"X-API-Key": "k1"}).status_code == 200

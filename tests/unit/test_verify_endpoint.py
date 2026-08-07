"""单元测试：REST 端点 POST /api/debug/verify"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.debug import router
from app.runtime.verifier import spec_store


@pytest.fixture(autouse=True)
def _isolate_spec_store():
    spec_store.clear()
    yield
    spec_store.clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestVerifyEndpoint:

    def test_verify_with_inline_spec_match(self, client):
        resp = client.post("/api/debug/verify", json={
            "actual": {"status_code": 200, "body": {"name": "Alice"}},
            "spec": {
                "kind": "api",
                "target": "GET /api/user",
                "expect": {"status": 200, "body_rules": {"name": "Alice"}},
            },
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is True
        assert body["diffs"] == []
        assert body["silent_failure"] is False

    def test_verify_silent_failure(self, client):
        """200 OK 但 body 不符合规范 → silent_failure=true"""
        resp = client.post("/api/debug/verify", json={
            "actual": {"status_code": 200, "body": {"success": True}},
            "spec": {
                "kind": "api",
                "expect": {"body_rules": {"success": False}},
            },
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is False
        assert body["silent_failure"] is True

    def test_verify_with_spec_id(self, client):
        spec_id = spec_store.create({
            "kind": "api",
            "target": "GET /api/user",
            "expect": {"status": 200, "body_rules": {"name": "Alice"}},
        })
        resp = client.post("/api/debug/verify", json={
            "actual": {"status_code": 200, "body": {"name": "Alice"}},
            "spec_id": spec_id,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is True
        assert body["spec_id"] == spec_id

    def test_verify_spec_id_not_found(self, client):
        resp = client.post("/api/debug/verify", json={
            "actual": {"status_code": 200, "body": {}},
            "spec_id": "no-such-spec",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is False
        assert "not found" in body["error"]

    def test_verify_no_spec(self, client):
        resp = client.post("/api/debug/verify", json={
            "actual": {"status_code": 200, "body": {}},
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is False
        assert "must provide" in body["error"]

    def test_verify_trace_id_passthrough(self, client):
        resp = client.post("/api/debug/verify", json={
            "actual": {"status_code": 200, "body": {}},
            "spec": {"kind": "api", "expect": {"status": 200}},
            "trace_id": "trace-abc",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == "trace-abc"

    def test_verify_ui_kind(self, client):
        resp = client.post("/api/debug/verify", json={
            "actual": {"state_changes": {"route_change": "/dashboard"}},
            "spec": {
                "kind": "ui",
                "target": "click #submit",
                "expect": {"state_change": {"route_change": "/dashboard"}},
            },
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is True

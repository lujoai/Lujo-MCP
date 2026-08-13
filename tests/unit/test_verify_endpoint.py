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


class TestVerifySchemaValidation:
    """P2-2: Pydantic Request Model schema validation 测试"""

    def test_verify_extra_fields_ignored(self, client):
        """旧客户端发送多余字段不应触发 422（extra=ignore）"""
        resp = client.post("/api/debug/verify", json={
            "actual": {"status_code": 200, "body": {}},
            "spec": {"kind": "api", "expect": {"status": 200}},
            "unknown_field": "should_be_ignored",
            "__proto__": "pollution_attempt",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is True

    def test_verify_missing_actual_returns_422(self, client):
        """缺少必填字段 actual 应返回 422"""
        resp = client.post("/api/debug/verify", json={
            "spec": {"kind": "api", "expect": {"status": 200}},
        })
        assert resp.status_code == 422

    def test_verify_ui_extra_fields_ignored(self, client):
        """verify/ui 旧客户端发送多余字段不应触发 422"""
        resp = client.post("/api/debug/verify/ui", json={
            "spec": {"kind": "ui", "target": "http://localhost", "expect": {}},
            "unknown_field": "ignored",
        })
        # handler 内部会因 playwright 未安装或 spec 格式返回错误，
        # 但不应是 422 schema validation 错误
        assert resp.status_code != 422

    def test_verify_ui_missing_all_optional_no_422(self, client):
        """verify/ui 所有字段可选，空对象不应 422"""
        resp = client.post("/api/debug/verify/ui", json={})
        assert resp.status_code != 422

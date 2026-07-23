"""单元测试：/api/spec REST CRUD"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.spec import router as spec_router
from app.mcp.verifier import spec_store


@pytest.fixture(autouse=True)
def _clean():
    """每个测试前清空 spec_store"""
    spec_store.clear()
    # clear() 已重置 _specs + _restored=False，设置 True 防止 list_specs 恢复历史数据
    spec_store._restored = True
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(spec_router)
    return TestClient(app)


class TestSpecAPI:

    def test_create_spec(self, client):
        resp = client.post("/api/spec", json={
            "kind": "api",
            "target": "GET /test",
            "expect": {"status": 200},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "spec_id" in data
        assert data["spec"]["kind"] == "api"

    def test_list_specs(self, client):
        client.post("/api/spec", json={"kind": "api", "target": "GET /a", "expect": {}})
        client.post("/api/spec", json={"kind": "ui", "target": "/page", "expect": {}})

        resp = client.get("/api/spec")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_list_specs_filter_by_kind(self, client):
        client.post("/api/spec", json={"kind": "api", "target": "GET /x", "expect": {}})
        client.post("/api/spec", json={"kind": "ui", "target": "/y", "expect": {}})

        resp = client.get("/api/spec?kind=ui")
        assert resp.json()["count"] == 1
        assert resp.json()["specs"][0]["kind"] == "ui"

    def test_get_spec(self, client):
        resp = client.post("/api/spec", json={"kind": "api", "target": "GET /z", "expect": {"status": 201}})
        spec_id = resp.json()["spec_id"]

        resp = client.get(f"/api/spec/{spec_id}")
        assert resp.status_code == 200
        assert resp.json()["target"] == "GET /z"

    def test_get_spec_not_found(self, client):
        resp = client.get("/api/spec/no-such-id")
        assert resp.status_code == 404

    def test_update_spec(self, client):
        resp = client.post("/api/spec", json={"kind": "api", "target": "GET /w", "expect": {}})
        spec_id = resp.json()["spec_id"]

        resp = client.patch(f"/api/spec/{spec_id}", json={"target": "GET /w-updated"})
        assert resp.status_code == 200
        assert resp.json()["target"] == "GET /w-updated"

    def test_update_spec_ignores_id(self, client):
        resp = client.post("/api/spec", json={"kind": "api", "target": "GET /v", "expect": {}})
        spec_id = resp.json()["spec_id"]

        resp = client.patch(f"/api/spec/{spec_id}", json={"target": "new", "id": "hijack"})
        assert resp.status_code == 200
        assert resp.json()["id"] == spec_id  # id 不可修改

    def test_delete_spec(self, client):
        resp = client.post("/api/spec", json={"kind": "api", "target": "GET /d", "expect": {}})
        spec_id = resp.json()["spec_id"]

        resp = client.delete(f"/api/spec/{spec_id}")
        assert resp.status_code == 200

        resp = client.get(f"/api/spec/{spec_id}")
        assert resp.status_code == 404

    def test_delete_spec_not_found(self, client):
        resp = client.delete("/api/spec/no-such-id")
        assert resp.status_code == 404


class TestSpecAPIErrorSanitization:
    """N4-FU-1: HTTPException detail 不得包含原始异常文本"""

    def test_create_spec_error_detail_excludes_exception(self, client, monkeypatch):
        sensitive = "DB_PASSWORD=hunter2"
        def _boom(_):
            raise RuntimeError(sensitive)
        monkeypatch.setattr(spec_store, "create", _boom)

        resp = client.post("/api/spec", json={"kind": "api", "target": "GET /x", "expect": {}})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "创建规范失败"
        assert sensitive not in resp.text

    def test_list_specs_error_detail_excludes_exception(self, client, monkeypatch):
        sensitive = "postgresql://user:pwd@host:5432/db"
        def _boom(*, kind=None, target=None):
            raise RuntimeError(sensitive)
        monkeypatch.setattr(spec_store, "list_specs", _boom)

        resp = client.get("/api/spec")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "列出规范失败"
        assert sensitive not in resp.text

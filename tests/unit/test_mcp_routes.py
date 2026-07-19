"""单元测试：MCP HTTP 路由会话语义"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mcp_routes import router
from app.mcp.transports.session import registry


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def setup_function():
    registry._sessions.clear()


def teardown_function():
    registry._sessions.clear()


def test_post_with_unknown_session_returns_404():
    client = _client()
    resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": "missing-session"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert resp.status_code == 404


def test_get_sse_with_unknown_session_returns_404():
    client = _client()
    resp = client.get(
        "/mcp",
        headers={"Mcp-Session-Id": "missing-session", "Accept": "text/event-stream"},
    )
    assert resp.status_code == 404


def test_delete_unknown_session_returns_404():
    client = _client()
    resp = client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": "missing-session"})
    assert resp.status_code == 404

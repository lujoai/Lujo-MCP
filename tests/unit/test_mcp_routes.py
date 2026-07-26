"""单元测试：MCP HTTP 路由会话语义"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mcp_routes import router
from app.mcp.transports.session import registry
from app.mcp.transports.sse import hub


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def setup_function():
    registry._sessions.clear()
    hub._queues.clear()


def teardown_function():
    registry._sessions.clear()
    hub._queues.clear()


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


@pytest.mark.asyncio
async def test_initialized_notification_publishes_ready_event():
    client = _client()
    init_resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    session_id = init_resp.headers["Mcp-Session-Id"]

    q = hub.subscribe(session_id)
    resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )

    assert resp.status_code == 202
    assert resp.headers["Mcp-Session-Id"] == session_id

    message = await asyncio.wait_for(q.get(), timeout=1)
    assert message["jsonrpc"] == "2.0"
    assert message["method"] == "notifications/session/ready"
    assert message["params"]["sessionId"] == session_id
    assert message["params"]["initialized"] is True


@pytest.mark.asyncio
async def test_post_sse_bridges_result_to_open_stream_subscriber():
    client = _client()
    init_resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    session_id = init_resp.headers["Mcp-Session-Id"]
    client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )

    q = hub.subscribe(session_id)
    resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id, "Accept": "text/event-stream"},
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
    )

    assert resp.status_code == 202
    assert resp.headers["Mcp-Session-Id"] == session_id

    message = await asyncio.wait_for(q.get(), timeout=1)
    assert message["id"] == 2
    assert message["result"] == {}


@pytest.mark.asyncio
async def test_delete_session_closes_sse_subscribers():
    client = _client()
    init_resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    session_id = init_resp.headers["Mcp-Session-Id"]
    q = hub.subscribe(session_id)

    resp = client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": session_id})

    assert resp.status_code == 204
    close_message = await asyncio.wait_for(q.get(), timeout=1)
    assert hub.is_close_event(close_message) is True
    assert hub.subscriber_count(session_id) == 0

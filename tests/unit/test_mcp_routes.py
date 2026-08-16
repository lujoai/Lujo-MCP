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


# ---------------------------------------------------------------------------
# P3-3: 会话驱逐策略（仅驱逐过期会话，全活跃拒绝新建）
# ---------------------------------------------------------------------------

class TestSessionRegistryEviction:
    def test_all_active_sessions_rejected_at_limit(self):
        from app.mcp.transports.session import (
            SessionRegistry,
            SessionLimitExceeded,
        )

        reg = SessionRegistry(max_sessions=2)
        reg.create()
        reg.create()
        with pytest.raises(SessionLimitExceeded):
            reg.create()
        # 原有活跃会话不被驱逐（防驱逐 DoS）
        assert len(reg._sessions) == 2

    def test_expired_sessions_evicted_at_limit(self):
        from app.mcp.transports.session import (
            SessionRegistry,
            _SESSION_TTL_SECONDS,
        )

        reg = SessionRegistry(max_sessions=2)
        s1 = reg.create()
        s2 = reg.create()
        # 将 s1 置为过期（超过 TTL）
        reg._sessions[s1.session_id].last_active -= _SESSION_TTL_SECONDS + 10

        s3 = reg.create()
        assert s1.session_id not in reg._sessions  # 过期者被驱逐
        assert s2.session_id in reg._sessions      # 活跃者保留
        assert s3.session_id in reg._sessions

    def test_expired_eviction_frees_multiple_slots(self):
        from app.mcp.transports.session import (
            SessionRegistry,
            _SESSION_TTL_SECONDS,
        )

        reg = SessionRegistry(max_sessions=2)
        s1 = reg.create()
        s2 = reg.create()
        reg._sessions[s1.session_id].last_active -= _SESSION_TTL_SECONDS + 10
        reg._sessions[s2.session_id].last_active -= _SESSION_TTL_SECONDS + 10

        s3 = reg.create()  # 两个过期槽位一次性释放
        s4 = reg.create()  # 释放后有空间，无需再驱逐
        assert s3.session_id in reg._sessions
        assert s4.session_id in reg._sessions
        assert len(reg._sessions) == 2


# ---------------------------------------------------------------------------
# TOOL_ROLE_REQUIREMENTS 覆盖校验
# ---------------------------------------------------------------------------

class TestToolRoleRequirementsCoverage:
    """确保所有注册工具都在 TOOL_ROLE_REQUIREMENTS 中有条目。"""

    def test_all_registered_tools_have_role_requirement(self):
        """register_all_tools() 注册的每个工具名都必须出现在 TOOL_ROLE_REQUIREMENTS 中。

        防止新增工具忘记添加角色要求导致 RBAC 静默失效。
        """
        from app.mcp.protocol.server import _tool_registry
        from app.mcp.tools import TOOL_ROLE_REQUIREMENTS, register_all_tools

        # 触发注册（幂等）
        register_all_tools()

        registered_names = set(_tool_registry.keys())
        covered_names = set(TOOL_ROLE_REQUIREMENTS.keys())

        missing = registered_names - covered_names
        assert not missing, (
            f"以下工具已注册但未在 TOOL_ROLE_REQUIREMENTS 中定义角色要求: {missing}。"
            f"未定义的工具将默认要求 admin 角色（fail-closed），但应显式声明。"
        )

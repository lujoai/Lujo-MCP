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


# ── FIX: P1-C4 —— MCP SSE 心跳（防反代空闲断流 + 刷新会话活跃时间）────
# 说明：与 test_dashboard_sse_stream.py 同理，SSE 流式测试直接驱动
# body_iterator（绕过 HTTP 层）——TestClient 的 httpx ASGITransport 与
# BaseHTTPMiddleware 在无限流场景下存在兼容性问题（会挂起）。


def _sse_request(session_id: str):
    """构造 GET /mcp SSE 请求桩（仅 headers 参与 mcp_get 逻辑）。"""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "headers": [
                (b"mcp-session-id", session_id.encode()),
                (b"accept", b"text/event-stream"),
            ],
        }
    )


@pytest.mark.asyncio
async def test_sse_stream_emits_heartbeat_and_refreshes_session(monkeypatch):
    """空闲 SSE 流周期性发送 `: ping` 注释行心跳，且心跳刷新会话 last_active。

    旧实现 q.get() 无限期等待：反代 60s 空闲即断流；纯监听会话 30 分钟后被
    TTL 清理踢下线。心跳间隔经模块常量缩短以避免真实等待。
    """
    from app.api import mcp_routes
    from app.mcp.transports.session import MCPSession

    monkeypatch.setattr(mcp_routes, "_SSE_HEARTBEAT_SECONDS", 0.05)
    sid = "hb-test-session"
    registry._sessions[sid] = MCPSession(session_id=sid)
    base_active = registry._sessions[sid].last_active
    await asyncio.sleep(0.01)

    response = None
    try:
        response = await mcp_routes.mcp_get(_sse_request(sid))
        assert response.status_code == 200
        assert "text/event-stream" in response.media_type

        saw_ping = False
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            if ": ping" in text:
                saw_ping = True
                break
        assert saw_ping, "空闲 SSE 流应周期性发送 : ping 心跳"

        # 心跳刷新了会话活跃时间（大于订阅前基准）
        assert registry._sessions[sid].last_active > base_active
    finally:
        # 终止流（close 事件让生成器退出 → finally unsubscribe）
        hub.close_session(sid)
        if response is not None:
            async for _ in response.body_iterator:
                pass
        registry._sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_sse_stream_delivers_messages_between_heartbeats(monkeypatch):
    """心跳不干扰正常消息投递：有消息时即时下发，非等满心跳间隔。"""
    from app.api import mcp_routes
    from app.mcp.transports.session import MCPSession

    monkeypatch.setattr(mcp_routes, "_SSE_HEARTBEAT_SECONDS", 15.0)
    sid = "hb-test-session-2"
    registry._sessions[sid] = MCPSession(session_id=sid)

    try:
        response = await mcp_routes.mcp_get(_sse_request(sid))
        it = response.body_iterator.__aiter__()

        first = await it.__anext__()
        first_text = first.decode() if isinstance(first, bytes) else first
        assert ": connected" in first_text

        # 发布一条 JSON-RPC 响应（同事件循环线程，sleep(0) 让投递回调执行）
        hub.publish(sid, {"jsonrpc": "2.0", "id": 2, "result": {}})
        await asyncio.sleep(0)

        second = await it.__anext__()
        second_text = second.decode() if isinstance(second, bytes) else second
        assert '"id"' in second_text and '"result"' in second_text
        # 不是心跳（即时投递，未等 15s 心跳间隔）
        assert ": ping" not in second_text
    finally:
        hub.close_session(sid)
        async for _ in response.body_iterator:
            pass
        registry._sessions.pop(sid, None)


def test_initialize_with_existing_session_creates_new():
    """P3-8: initialize 携带已有 session_id 时必须新建会话，而非复用（防会话固定/通知流劫持）。"""
    client = _client()
    init_resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    first_id = init_resp.headers["Mcp-Session-Id"]
    assert first_id

    # 携带已有 session_id 再次 initialize → 必须新建不同会话
    init_resp2 = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": first_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
    )
    second_id = init_resp2.headers["Mcp-Session-Id"]
    assert second_id != first_id
    # 原会话未被复用/删除，仍独立存在
    assert registry.get(first_id) is not None
    assert registry.get(second_id) is not None


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

    def test_cleanup_returns_cleaned_sid_list(self):
        """FIX P3-14: cleanup 返回被清理的 sid 列表（而非 int 数量），供 SSE hub 关闭"""
        from app.mcp.transports.session import (
            SessionRegistry,
            _SESSION_TTL_SECONDS,
        )

        reg = SessionRegistry()
        s1 = reg.create()
        s2 = reg.create()
        reg._sessions[s1.session_id].last_active -= _SESSION_TTL_SECONDS + 10

        cleaned = reg.cleanup(ttl_seconds=_SESSION_TTL_SECONDS)
        assert cleaned == [s1.session_id]
        assert s1.session_id not in reg._sessions
        assert s2.session_id in reg._sessions

    def test_cleanup_returns_empty_list_when_none_expired(self):
        from app.mcp.transports.session import SessionRegistry

        reg = SessionRegistry()
        reg.create()
        assert reg.cleanup(ttl_seconds=1800) == []


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

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


def test_delete_without_session_header_returns_400():
    """FIX(v0.7.1-b1-6) 回归：缺 Mcp-Session-Id 的 DELETE 返回 400（此前静默 204）。"""
    client = _client()
    resp = client.request("DELETE", "/mcp")
    assert resp.status_code == 400
    assert "Mcp-Session-Id" in resp.json()["detail"]


def test_post_oversized_body_returns_413(monkeypatch):
    """FIX(v0.7.1-b4-5)：超限请求体被拒（413），不再无界读入内存。"""
    from app.api import mcp_routes

    monkeypatch.setattr(mcp_routes, "_MCP_MAX_BODY_BYTES", 100)
    client = _client()

    # 构造超 Content-Length 的请求（原始 body 超过 100 字节）
    resp = client.request(
        "POST",
        "/mcp",
        headers={"Content-Type": "application/json"},
        content=b'{"jsonrpc":"2.0","id":1,"method":"' + b"x" * 200 + b'"}',
    )
    assert resp.status_code == 413


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


# ---------------------------------------------------------------------------
# FIX: R7-A3 —— SSE 响应统一补缓冲控制头（Cache-Control / X-Accel-Buffering）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_get_sse_stream_has_buffer_control_headers():
    """GET /mcp SSE 流必须带 Cache-Control: no-cache 与 X-Accel-Buffering: no。

    缺失时 nginx 默认 proxy_buffering on 会攒批延迟心跳与事件（dashboard
    流有头、MCP 流没有的修复不对称，R7-A3）。
    """
    from app.api import mcp_routes
    from app.mcp.transports.session import MCPSession

    sid = "sse-header-session"
    registry._sessions[sid] = MCPSession(session_id=sid)
    try:
        response = await mcp_routes.mcp_get(_sse_request(sid))
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"
        # 关闭生成器（触发 finally 取消订阅），避免悬挂任务
        await response.body_iterator.aclose()
    finally:
        registry._sessions.pop(sid, None)


def test_mcp_post_sse_fallback_has_buffer_control_headers():
    """POST /mcp（Accept: SSE、无订阅者回退为流式响应）同样必须带头。"""
    client = _client()
    init_resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    session_id = init_resp.headers["Mcp-Session-Id"]
    client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id, "Accept": "text/event-stream"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    # 无订阅者 → publish False → 回退为单事件 SSE 流
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Accel-Buffering"] == "no"


# ── v0.7.3: HTTP tools/list 暴露 Agent-facing 工具并过滤 SDK 上报工具 ──


def _initialized_session(client):
    """完成 initialize + initialized 握手，返回 (client, session_id)。"""
    init_resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    session_id = init_resp.headers["Mcp-Session-Id"]
    client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return session_id


def test_http_tools_list_contains_core_agent_tools():
    from app.mcp.tools import register_all_tools
    from app.mcp.protocol.server import _is_tool_available, _tool_registry

    register_all_tools()
    client = _client()
    session_id = _initialized_session(client)
    resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}

    for core in ("diagnose_issue", "list_recent_traces", "search_logs",
                 "context", "trace", "stacktrace", "verify", "debug"):
        assert core in names, f"HTTP tools/list 缺少核心工具 {core}"

    # verify_ui 依赖可选的 Playwright：依赖存在时必须公开，未安装时应被
    # availability 过滤，但 registry 仍保留、tools/call 仍可用。
    if _is_tool_available(_tool_registry["verify_ui"]):
        assert "verify_ui" in names
    else:
        assert "verify_ui" not in names


def test_http_tools_list_filters_sdk_ingest_tools():
    """SDK 上报工具不进 HTTP tools/list（与 stdio 口径一致），tools/call 仍可调用。"""
    import json as _json

    from app.mcp.tools import register_all_tools

    register_all_tools()
    client = _client()
    session_id = _initialized_session(client)
    resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "ingest_error" not in names
    assert "ingest_console" not in names
    assert "ingest_network" not in names
    assert "ingest_silent_failure" not in names

    # 被过滤的工具按名调用照常执行
    call_resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ingest_error", "arguments": {"exc_type": "E", "message": "m"}},
        },
    )
    assert call_resp.status_code == 200
    payload = _json.loads(call_resp.json()["result"]["content"][0]["text"])
    assert payload["saved"] is True


def test_http_tools_call_diagnose_issue_empty_result_is_guided():
    """HTTP 全链路：空数据时 diagnose_issue 返回 found=false + 引导（非空对象）。"""
    import json as _json

    from app.mcp.tools import register_all_tools
    from app.runtime.core.storage import factory as _storage_factory

    # memory trace 存储是进程级单例：前序测试（如 ingest_error 调用）可能写入
    # trace 数据，重置后保证本用例从「零数据」起步（errors._recent 由 conftest 清）
    _storage_factory._trace_store = None
    register_all_tools()
    client = _client()
    session_id = _initialized_session(client)
    resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "diagnose_issue", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    payload = _json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload["found"] is False
    assert payload["setup_hint"]
    assert payload["next_step"]

# ---------------------------------------------------------------------------
# FIX: B09 —— 非法 initialize 请求创建 session 后未回收
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []},
        {"jsonrpc": "1.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": {"a": 1}, "method": "initialize", "params": {}},
    ],
    ids=["params-not-object", "jsonrpc-invalid", "id-invalid"],
)
def test_illegal_initialize_reclaims_session(payload):
    """非法 initialize（params 非对象 / jsonrpc 非法 / id 非法）不泄漏 session 且无 Session 头。

    原实现：initialize 先 registry.create() 建会话，dispatch_raw 返回 error 后 session 残留，
    响应仍带 Mcp-Session-Id，重复请求持续消耗 10000 上限。
    新实现：dispatch_raw 返回含 "error" 的响应即回收本次新建的 created_session_id，
    并把对外 session_id 置 None，使响应不再携带 Mcp-Session-Id。
    断言语义：registry 无残留 + 响应头不含 Mcp-Session-Id，锁定「非法 initialize 不保留会话」。
    """
    client = _client()
    resp = client.post("/mcp", json=payload)
    body = resp.json()
    assert body["error"]["code"] == -32600
    assert "Mcp-Session-Id" not in resp.headers
    assert len(registry._sessions) == 0


def test_repeated_illegal_initialize_do_not_exhaust_sessions():
    """连续多次非法 initialize 不消耗 session 容量。

    原实现：每次非法 initialize 都残留一个 session，20 次后 registry 有 20 个残留。
    新实现：每次失败即回收，循环后存活 session 数为 0。
    断言语义：失败请求不累计占据会话上限，锁定「非法 initialize 不消耗容量」。
    """
    client = _client()
    for _ in range(20):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []},
        )
        assert resp.json()["error"]["code"] == -32600
    assert len(registry._sessions) == 0


def test_dispatch_raw_error_reclaims_session(monkeypatch):
    """dispatch_raw 返回协议错误（含 error 键）时回收临时 session。

    原实现：session 先建后不回收，残留 1 个。
    新实现：任意返回含 "error" 的响应都回收本次 created_session_id 且不设 Session 头。
    断言语义：只要 dispatch_raw 返回 error，session 数回到 0，锁定「返回 error 即回收」。
    """
    import app.api.mcp_routes as mcp_routes

    async def fake_dispatch(raw):
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "boom"}}

    monkeypatch.setattr(mcp_routes, "dispatch_raw", fake_dispatch)
    client = _client()
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert resp.json()["error"]["code"] == -32600
    assert "Mcp-Session-Id" not in resp.headers
    assert len(registry._sessions) == 0


def test_dispatch_raw_exception_reclaims_session(monkeypatch):
    """dispatch_raw 抛异常（走 500 分支）时回收临时 session，且不掩错、不回显异常原文。

    原实现：exception 分支只 logger.exception + 返回 500，已建 session 残留 1 个。
    新实现：exception 分支在返回 500 前回收本次 created_session_id，保留统一文案。
    断言语义：抛异常后 session 数回到 0、状态码仍 500、错误码仍 -32603、响应不含异常原文。
    """
    import app.api.mcp_routes as mcp_routes

    def fake_dispatch(raw):
        raise RuntimeError("internal-boom-marker")

    monkeypatch.setattr(mcp_routes, "dispatch_raw", fake_dispatch)
    client = _client()
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == -32603
    assert "internal-boom-marker" not in resp.text
    assert "Mcp-Session-Id" not in resp.headers
    assert len(registry._sessions) == 0


def test_illegal_initialize_notification_keeps_202_semantics():
    """无 id 的非法 initialize 保持通知语义：HTTP 202、空响应体、无 Mcp-Session-Id。

    原实现：无 id 非法 initialize 走通知分支返回 202 空响应，但 session 已建且响应带
    Mcp-Session-Id 头，造成泄漏。
    新实现：失败即回收并把 session_id 置 None，仍走既有通知分支（202 空响应），
    但不再设置 Mcp-Session-Id 头，session 数回到 0。
    断言语义：202 + 空 body + 无 Session 头 + registry 空，锁定通知语义不被破坏且不泄漏。
    """
    client = _client()
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "initialize", "params": []}
    )
    assert resp.status_code == 202
    assert resp.content == b""
    assert "Mcp-Session-Id" not in resp.headers
    assert len(registry._sessions) == 0


def test_legal_initialize_retains_session():
    """合法 initialize 仍创建并保留 session，且返回 Mcp-Session-Id（回归保护）。

    原实现与新实现行为一致：合法 initialize 返回 200 + Mcp-Session-Id，session 保留。
    断言语义：合法路径不因 B09 修复而改变，锁定「合法 initialize 行为保持」。
    """
    client = _client()
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert resp.status_code == 200
    sid = resp.headers["Mcp-Session-Id"]
    assert sid
    assert registry.get(sid) is not None
    assert "result" in resp.json()


def test_illegal_then_legal_initialize_succeeds():
    """非法 initialize 后接合法 initialize 仍成功，且存活 session 仅 1 个。

    原实现：非法 initialize 残留 1 个 session，合法 initialize 再建 1 个，共 2 个。
    新实现：非法不残留，合法 initialize 后存活 session 数为 1。
    断言语义：合法 initialize 在非法请求之后仍能建立会话，锁定「限流/容器不被失败请求污染」。
    """
    client = _client()
    bad = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []}
    )
    assert bad.json()["error"]["code"] == -32600

    good = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}
    )
    assert good.status_code == 200
    sid = good.headers["Mcp-Session-Id"]
    assert sid and registry.get(sid) is not None
    assert len(registry._sessions) == 1


@pytest.mark.asyncio
async def test_illegal_initialize_does_not_delete_existing_session():
    """非法 initialize 只回收本次新建会话，不删除已有合法 session 及其 SSE 订阅。

    原实现虽泄漏但不误删（本用例改前也通过），用于防止修复引入「误删既有会话」的新缺陷。
    新实现：回收严格限定本次 created_session_id，已有 session 与其订阅原样保留。
    断言语义：已有 session 仍可 get、订阅数不变，锁定「回收隔离性」。
    """
    client = _client()
    good = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    existing_sid = good.headers["Mcp-Session-Id"]
    q = hub.subscribe(existing_sid)
    assert hub.subscriber_count(existing_sid) == 1

    bad = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": []}
    )
    assert bad.json()["error"]["code"] == -32600

    assert registry.get(existing_sid) is not None
    assert hub.subscriber_count(existing_sid) == 1
    hub.unsubscribe(existing_sid, q)

# ---------------------------------------------------------------------------
# FIX: B25 —— HTTP 边界：非字符串 tool name 不再 500，统一 400 + -32602
# ---------------------------------------------------------------------------


class TestB25HttpToolNameValidation:
    """B25: HTTP 边界 —— 非字符串 tool name 不再 500，统一 400 + -32602。"""

    @pytest.mark.parametrize(
        "name_value",
        [["x"], {"a": 1}, None, 42, True],
        ids=["list", "dict", "null", "number", "boolean"],
    )
    def test_non_string_name_returns_400_not_500(self, name_value):
        """非字符串 name → HTTP 400 + -32602（非 500）。

        原实现：list/dict → TOOL_ROLE_REQUIREMENTS.get 抛 TypeError → 500；
        null/number/boolean → 通过 RBAC 后协议层 -32601（误判）。
        新实现：RBAC 前校验，统一 400 + -32602。
        断言锁定：HTTP 不返回 500，错误码为 -32602，不泄漏 TypeError/unhashable。
        """
        client = _client()
        session_id = _initialized_session(client)
        resp = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": name_value, "arguments": {}}},
        )
        assert resp.status_code != 500
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32602
        assert "TypeError" not in resp.text
        assert "unhashable" not in resp.text

    def test_missing_name_with_admin_returns_32601(self, monkeypatch):
        """缺失 name + rbac_enabled=False → -32601（保持未知工具语义）。

        原实现与新实现一致：缺失 name → "" → 默认 admin required → admin 通过 → 协议层 -32601。
        断言锁定：B25 不把缺失误判为 malformed params，RBAC 状态显式设置。
        """
        from app.config import settings
        monkeypatch.setattr(settings, "rbac_enabled", False)

        client = _client()
        session_id = _initialized_session(client)
        resp = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"arguments": {}}},
        )
        assert resp.json()["error"]["code"] == -32601

    def test_unknown_string_with_admin_returns_32601(self, monkeypatch):
        """未知字符串 + rbac_enabled=False → -32601（保持）。

        原实现与新实现一致：未知字符串 → 默认 admin required → admin 通过 → 协议层 -32601。
        断言锁定：B25 不把未知字符串误判为 malformed params。
        """
        from app.config import settings
        monkeypatch.setattr(settings, "rbac_enabled", False)

        client = _client()
        session_id = _initialized_session(client)
        resp = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "b25-no-such", "arguments": {}}},
        )
        assert resp.json()["error"]["code"] == -32601

    def test_rbac_fail_closed_still_403_for_unknown_name(self, monkeypatch):
        """RBAC fail-closed 回归：rbac_enabled=True 且角色不足 → 403。

        原实现与新实现一致：未知 name 默认 admin required，viewer → 403。
        断言锁定：B25 修复不绕过 RBAC fail-closed。
        """
        from app.config import settings
        monkeypatch.setattr(settings, "rbac_enabled", True)

        client = _client()
        session_id = _initialized_session(client)
        resp = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "b25-no-such", "arguments": {}}},
        )
        assert resp.status_code == 403

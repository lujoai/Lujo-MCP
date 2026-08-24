"""集成测试：Dashboard SSE 实时推送端点 (/api/dashboard/stream)

覆盖：
- 端点关闭时返回 503（SSE 功能未启用）
- SSE 流正确建立并发送 connected 事件
- 投递 dashboard_changed 事件后，SSE 流中收到事件
- close_all 后流自然终止
- 订阅清理（无死订阅泄露）

注意：SSE 流式测试使用 body_iterator 直接驱动（绕过 HTTP 层），
因为 Starlette BaseHTTPMiddleware 与 httpx ASGITransport 在 SSE 场景下存在
"Unexpected message received: http.request" 兼容性问题。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.api.dashboard_events import dashboard_hub


@pytest.fixture
def sse_settings(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_sse_enabled", True)
    monkeypatch.setattr(settings, "api_key", "test-key-for-sse")
    yield
    dashboard_hub._subs.clear()


# ── 非流式端点测试 ──


def test_stream_returns_503_when_disabled(monkeypatch):
    """dashboard_sse_enabled=False 时返回 503。"""
    monkeypatch.setattr(settings, "dashboard_sse_enabled", False)
    monkeypatch.setattr(settings, "api_key", None)
    client = TestClient(app)
    resp = client.get("/api/dashboard/stream")
    assert resp.status_code == 503


# ── SSE 流式测试（直接驱动 body_iterator） ──


@pytest.mark.asyncio
async def test_stream_connected_event(sse_settings):
    """SSE 流建立后首先收到 :connected 注释行。"""
    from app.api.dashboard import dashboard_stream
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer test-key-for-sse")],
            "query_params": {},
        }
    )
    response = await dashboard_stream(request)
    assert response.status_code == 200
    assert "text/event-stream" in response.media_type
    assert response.headers["cache-control"] == "no-cache"

    # 读取 connected 事件（需先发关闭信号，否则无限流）
    dashboard_hub.close_all()
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    joined = "".join(chunks)
    assert ": connected" in joined


@pytest.mark.asyncio
async def test_stream_receives_events(sse_settings):
    """投递 dashboard_changed 事件后，SSE 流中收到事件。"""
    from app.api.dashboard import dashboard_stream
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer test-key-for-sse")],
            "query_params": {},
        }
    )
    response = await dashboard_stream(request)
    assert dashboard_hub.subscriber_count() == 1

    # 投递真实事件 + 关闭信号
    dashboard_hub.publish({"type": "dashboard_changed", "source": "trace"})
    dashboard_hub.close_all()

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    joined = "".join(chunks)
    assert ": connected" in joined
    assert "dashboard_changed" in joined
    # 流自然终止后订阅已清理
    assert dashboard_hub.subscriber_count() == 0


@pytest.mark.asyncio
async def test_stream_cleans_up_on_close(sse_settings):
    """close 信号终止流后，订阅被清理，不留死订阅。"""
    from app.api.dashboard import dashboard_stream
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer test-key-for-sse")],
            "query_params": {},
        }
    )
    response = await dashboard_stream(request)
    assert dashboard_hub.subscriber_count() == 1

    dashboard_hub.close_all()

    async for _ in response.body_iterator:
        pass
    assert dashboard_hub.subscriber_count() == 0

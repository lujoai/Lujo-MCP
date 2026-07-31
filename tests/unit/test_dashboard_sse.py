"""单元测试：Dashboard 实时 SSE 推送（DASH-SSE-001）

覆盖：
- DashboardEventBus：subscribe/publish/unsubscribe/close_all + 跨线程投递 + 队列满丢旧
- broadcast_dashboard_event：功能关闭 / 无订阅者 no-op 降级
- GET /api/dashboard/stream：关闭时 503、启用时 SSE 流 + connected 头 + 事件投递 + close 终止
- invalidate_cache 广播钩子：启用 + 有订阅者时投递 dashboard_changed 事件
"""

import asyncio
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dashboard import invalidate_cache, router
from app.api.dashboard_events import (
    _CLOSE_EVENT,
    _DashboardSubscription,
    DashboardEventBus,
    broadcast_dashboard_event,
    dashboard_hub,
)
from app.config import Settings, settings


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def setup_function():
    dashboard_hub._subs.clear()


def teardown_function():
    dashboard_hub._subs.clear()


# ── 配置默认值 ──

def test_config_default_disabled():
    s = Settings()
    assert s.dashboard_sse_enabled is False


# ── DashboardEventBus 基础语义 ──

class TestDashboardEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_publish_delivers(self):
        bus = DashboardEventBus()
        q = bus.subscribe()
        assert bus.subscriber_count() == 1
        delivered = bus.publish({"type": "dashboard_changed"})
        assert delivered == 1
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["type"] == "dashboard_changed"

    @pytest.mark.asyncio
    async def test_publish_no_subscribers_returns_zero(self):
        bus = DashboardEventBus()
        assert bus.publish({"type": "x"}) == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        bus = DashboardEventBus()
        q = bus.subscribe()
        assert bus.subscriber_count() == 1
        bus.unsubscribe(q)
        assert bus.subscriber_count() == 0
        assert bus.publish({"type": "x"}) == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_unknown_queue_is_noop(self):
        bus = DashboardEventBus()
        other: asyncio.Queue = asyncio.Queue()
        # 未订阅的 queue 调用 unsubscribe 不应抛异常
        bus.unsubscribe(other)
        assert bus.subscriber_count() == 0

    @pytest.mark.asyncio
    async def test_publish_from_thread_is_threadsafe(self):
        """publish 从非事件循环线程调用，仍能跨线程投递到订阅者。"""
        bus = DashboardEventBus()
        q = bus.subscribe()
        done = threading.Event()

        def pusher():
            bus.publish({"type": "from_thread"})
            done.set()

        t = threading.Thread(target=pusher)
        t.start()
        msg = await asyncio.wait_for(q.get(), timeout=2)
        done.wait(2)
        t.join(2)
        assert msg["type"] == "from_thread"

    @pytest.mark.asyncio
    async def test_put_nowait_drops_oldest_when_full(self):
        """队列满时丢弃最旧事件，保最新（实时性优先）。"""
        small_q: asyncio.Queue = asyncio.Queue(maxsize=2)
        sub = _DashboardSubscription(queue=small_q, loop=asyncio.get_running_loop())
        DashboardEventBus._put_nowait(sub, {"i": 1})
        DashboardEventBus._put_nowait(sub, {"i": 2})
        assert small_q.full()
        # 溢出 → 丢最旧（1），保最新（2,3）
        DashboardEventBus._put_nowait(sub, {"i": 3})
        items = []
        while not small_q.empty():
            items.append(small_q.get_nowait())
        assert [it["i"] for it in items] == [2, 3]

    @pytest.mark.asyncio
    async def test_close_all_delivers_close_event_and_clears(self):
        bus = DashboardEventBus()
        q = bus.subscribe()
        assert bus.subscriber_count() == 1
        n = bus.close_all()
        assert n == 1
        assert bus.subscriber_count() == 0
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert bus.is_close_event(msg)

    def test_format_event_format(self):
        ev = DashboardEventBus.format_event({"type": "x"})
        assert ev.startswith("event: message\ndata: ")
        assert ev.endswith("\n\n")
        assert '"type": "x"' in ev

    def test_is_close_event(self):
        assert DashboardEventBus.is_close_event(_CLOSE_EVENT) is True
        assert DashboardEventBus.is_close_event({"type": "x"}) is False


# ── broadcast_dashboard_event 降级矩阵 ──

class TestBroadcastDegradation:
    def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_sse_enabled", False)
        # 无订阅者 + 功能关闭 → no-op，不抛异常
        broadcast_dashboard_event({"type": "x"})
        assert dashboard_hub.subscriber_count() == 0

    @pytest.mark.asyncio
    async def test_noop_when_enabled_but_no_subscribers(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_sse_enabled", True)
        assert dashboard_hub.subscriber_count() == 0
        broadcast_dashboard_event({"type": "x"})
        assert dashboard_hub.subscriber_count() == 0

    @pytest.mark.asyncio
    async def test_broadcast_delivers_when_enabled_with_subscriber(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_sse_enabled", True)
        q = dashboard_hub.subscribe()
        try:
            broadcast_dashboard_event({"type": "dashboard_changed", "source": "trace"})
            msg = await asyncio.wait_for(q.get(), timeout=1)
            assert msg["type"] == "dashboard_changed"
            assert msg["source"] == "trace"
        finally:
            dashboard_hub.unsubscribe(q)


# ── invalidate_cache 广播钩子 ──

class TestInvalidateCacheHook:
    @pytest.mark.asyncio
    async def test_broadcasts_when_enabled_with_subscriber(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_sse_enabled", True)
        q = dashboard_hub.subscribe()
        try:
            invalidate_cache(source="trace")
            msg = await asyncio.wait_for(q.get(), timeout=1)
            assert msg["type"] == "dashboard_changed"
            assert msg["source"] == "trace"
        finally:
            dashboard_hub.unsubscribe(q)

    def test_no_broadcast_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_sse_enabled", False)
        # 功能关闭时 invalidate_cache 不应广播（broadcast_dashboard_event 内部短路）
        # 不订阅即可，主要验证不抛异常
        invalidate_cache()
        assert dashboard_hub.subscriber_count() == 0


# ── GET /api/dashboard/stream 端点 ──

class TestStreamEndpoint:
    def test_returns_503_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_sse_enabled", False)
        client = TestClient(_app())
        resp = client.get("/api/dashboard/stream")
        assert resp.status_code == 503
        assert "未启用" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_enabled_handler_yields_connected_event_and_closes(self, monkeypatch):
        """直接驱动 StreamingResponse.body_iterator，确定性验证端点契约。

        绕过 HTTP 层（避免无限流 + 跨线程时序死锁）：
        1. 调用 handler 拿到 StreamingResponse；
        2. 发布一个真实事件 + 关闭信号；
        3. 消费 body_iterator，断言 connected 头 + 事件 + 自然终止 + 订阅清理。
        """
        monkeypatch.setattr(settings, "dashboard_sse_enabled", True)
        from app.api.dashboard import dashboard_stream
        from starlette.requests import Request

        request = Request({"type": "http", "headers": [], "query_params": {}})
        response = await dashboard_stream(request)

        assert response.status_code == 200
        assert "text/event-stream" in response.media_type
        assert response.headers["cache-control"] == "no-cache"
        # 订阅已建立
        assert dashboard_hub.subscriber_count() == 1

        # 投递真实事件 + 关闭信号（call_soon_threadsafe 在 generator await 时生效）
        dashboard_hub.publish({"type": "dashboard_changed", "source": "trace"})
        dashboard_hub.close_all()

        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

        joined = "".join(chunks)
        assert ": connected" in joined
        assert "dashboard_changed" in joined
        # 流自然终止后订阅已清理（handler finally: unsubscribe）
        assert dashboard_hub.subscriber_count() == 0

    @pytest.mark.asyncio
    async def test_handler_unsubscribes_on_close(self, monkeypatch):
        """close 信号终止流后，订阅被清理，不留死订阅。"""
        monkeypatch.setattr(settings, "dashboard_sse_enabled", True)
        from app.api.dashboard import dashboard_stream
        from starlette.requests import Request

        request = Request({"type": "http", "headers": [], "query_params": {}})
        response = await dashboard_stream(request)
        assert dashboard_hub.subscriber_count() == 1

        dashboard_hub.close_all()

        async for _ in response.body_iterator:
            pass

        assert dashboard_hub.subscriber_count() == 0


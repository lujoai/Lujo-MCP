"""DashboardEventBus 线程安全测试 —— 验证跨线程并发访问无竞态"""
import threading
import pytest
from app.api.dashboard_events import DashboardEventBus, _CLOSE_EVENT


@pytest.fixture
def bus():
    return DashboardEventBus()


@pytest.mark.asyncio
async def test_concurrent_subscribe_unsubscribe(bus):
    """并发 subscribe/unsubscribe 不抛异常"""
    qs = []
    for _ in range(10):
        q = bus.subscribe()
        qs.append(q)

    def _unsubscribe_all():
        for q in qs:
            bus.unsubscribe(q)

    thread = threading.Thread(target=_unsubscribe_all)
    thread.start()
    thread.join()

    assert bus.subscriber_count() == 0


@pytest.mark.asyncio
async def test_concurrent_publish_no_crash(bus):
    """并发 publish 不导致 list modified during iteration"""
    q = bus.subscribe()
    errors = []

    def _publish_loop():
        for i in range(100):
            try:
                bus.publish({"event": i})
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=_publish_loop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"并发 publish 出错: {errors}"
    bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_close_all_during_publish(bus):
    """close_all 与 publish 并发不崩溃"""
    for _ in range(5):
        bus.subscribe()
    errors = []

    def _publish():
        for i in range(50):
            try:
                bus.publish({"event": i})
            except Exception as e:
                errors.append(e)

    def _close():
        bus.close_all()

    t1 = threading.Thread(target=_publish)
    t2 = threading.Thread(target=_close)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0, f"close_all+publish 并发出错: {errors}"


@pytest.mark.asyncio
async def test_subscriber_count_after_close(bus):
    """close_all 后 subscriber_count 为 0"""
    bus.subscribe()
    bus.subscribe()
    assert bus.subscriber_count() == 2
    bus.close_all()
    assert bus.subscriber_count() == 0


@pytest.mark.asyncio
async def test_publish_to_no_subscribers(bus):
    """无订阅者时 publish 返回 0"""
    assert bus.publish({"event": "test"}) == 0


@pytest.mark.asyncio
async def test_publish_delivery_count(bus):
    """publish 返回投递数"""
    bus.subscribe()
    bus.subscribe()
    bus.subscribe()
    n = bus.publish({"event": "test"})
    assert n == 3


def test_is_close_event():
    """is_close_event 正确识别关闭信号"""
    assert DashboardEventBus.is_close_event(_CLOSE_EVENT)
    assert not DashboardEventBus.is_close_event({"event": "test"})

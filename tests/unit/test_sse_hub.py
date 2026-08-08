"""SSEHub 线程安全测试 —— 验证跨线程并发访问无竞态"""
import asyncio
import threading
import pytest
from app.mcp.transports.sse import SSEHub, _CLOSE_EVENT


@pytest.fixture
def hub():
    return SSEHub()


@pytest.fixture
def mock_session(monkeypatch):
    """直接操作 registry 的内部字典注入 session，绕过 SEC-04 校验"""
    from app.mcp.transports import session as session_module
    sid = "test-session-001"
    session_module.registry._sessions[sid] = session_module.MCPSession(session_id=sid)
    yield sid
    session_module.registry._sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_concurrent_subscribe_unsubscribe(hub, mock_session):
    """并发 subscribe/unsubscribe 不抛异常"""
    qs = []
    for _ in range(10):
        q = hub.subscribe(mock_session)
        qs.append(q)

    def _unsubscribe_all():
        for q in qs:
            hub.unsubscribe(mock_session, q)

    thread = threading.Thread(target=_unsubscribe_all)
    thread.start()
    thread.join()

    assert hub.subscriber_count(mock_session) == 0


@pytest.mark.asyncio
async def test_concurrent_publish_no_crash(hub, mock_session):
    """并发 publish 不导致 list modified during iteration"""
    q = hub.subscribe(mock_session)
    errors = []

    def _publish_loop():
        for i in range(100):
            try:
                hub.publish(mock_session, {"msg": i})
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=_publish_loop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"并发 publish 出错: {errors}"
    hub.unsubscribe(mock_session, q)


@pytest.mark.asyncio
async def test_close_session_during_publish(hub, mock_session):
    """close_session 与 publish 并发不崩溃"""
    hub.subscribe(mock_session)
    errors = []

    def _publish():
        for i in range(50):
            try:
                hub.publish(mock_session, {"msg": i})
            except Exception as e:
                errors.append(e)

    def _close():
        for _ in range(20):
            hub.close_session(mock_session)

    t1 = threading.Thread(target=_publish)
    t2 = threading.Thread(target=_close)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0, f"close+publish 并发出错: {errors}"


@pytest.mark.asyncio
async def test_subscriber_count_after_close(hub, mock_session):
    """close_session 后 subscriber_count 为 0"""
    hub.subscribe(mock_session)
    hub.subscribe(mock_session)
    assert hub.subscriber_count(mock_session) == 2
    hub.close_session(mock_session)
    assert hub.subscriber_count(mock_session) == 0


@pytest.mark.asyncio
async def test_is_close_event(hub):
    """is_close_event 正确识别关闭信号"""
    assert hub.is_close_event(_CLOSE_EVENT)
    assert not hub.is_close_event({"msg": "hello"})


@pytest.mark.asyncio
async def test_publish_drops_oldest_when_queue_full(hub, mock_session):
    """FIX: P1-10a 有界队列 —— 超过 maxsize 时丢最旧一条，防止慢消费客户端无界增长"""
    q = hub.subscribe(mock_session)

    total = SSEHub._QUEUE_MAXSIZE + 50
    for i in range(total):
        hub.publish(mock_session, {"seq": i})
        # 让事件循环执行 call_soon_threadsafe 排队的回调
        await asyncio.sleep(0)

    # 队列保持有界
    assert q.qsize() == SSEHub._QUEUE_MAXSIZE

    # 满时丢最旧：队首应是 total - maxsize，队尾是最后发布的消息
    first = await q.get()
    assert first["seq"] == total - SSEHub._QUEUE_MAXSIZE

    tail = None
    while not q.empty():
        tail = q.get_nowait()
    assert tail is not None
    assert tail["seq"] == total - 1
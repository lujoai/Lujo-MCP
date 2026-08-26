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
    """并发 subscribe/unsubscribe 不抛异常（订阅数保持在每 session 上限内）"""
    qs = []
    for _ in range(5):  # 每 session 订阅上限 _MAX_SUBSCRIBERS_PER_SESSION=5
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
async def test_subscribe_exceeds_per_session_limit(hub, mock_session):
    """同一 session 订阅数达上限后拒绝（P3-7 防无限长连接）"""
    for _ in range(5):  # 上限内可订阅
        hub.subscribe(mock_session)
    with pytest.raises(PermissionError):  # 第 6 个被拒
        hub.subscribe(mock_session)


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
async def test_subscribe_after_close_creates_fresh_subscription(hub, mock_session):
    """P3-11: close_session 后重新 subscribe 应新建订阅，不因残留列表丢订阅"""
    hub.close_session(mock_session)
    q = hub.subscribe(mock_session)
    assert hub.subscriber_count(mock_session) == 1
    hub.unsubscribe(mock_session, q)
    assert hub.subscriber_count(mock_session) == 0


@pytest.mark.asyncio
async def test_subscribe_during_close_session_no_lost_sub(hub, mock_session):
    """P3-11: 事件循环线程 subscribe 与 worker 线程 close_session 并发，订阅不丢失、不崩溃"""
    stop = False
    errors = []

    def _closer():
        try:
            while not stop:
                hub.close_session(mock_session)
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=_closer)
    t.start()
    try:
        for _ in range(300):
            q = hub.subscribe(mock_session)
            hub.unsubscribe(mock_session, q)
            await asyncio.sleep(0)
    finally:
        stop = True
        t.join()

    assert len(errors) == 0, f"subscribe+close 并发出错: {errors}"
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

@pytest.mark.asyncio
async def test_close_session_drops_oldest_when_queue_full(hub, mock_session):
    """FIX: close_session 队列满时通过 _publish_locked 丢弃最旧消息放入 _CLOSE_EVENT，不抛 QueueFull"""
    q = hub.subscribe(mock_session)

    # 填满队列至 _QUEUE_MAXSIZE (256)
    for i in range(SSEHub._QUEUE_MAXSIZE):
        q.put_nowait({"msg": f"data-{i}"})

    assert q.full()
    assert q.qsize() == SSEHub._QUEUE_MAXSIZE

    # close_session 应该安全调度 _publish_locked，丢弃最旧数据并将 _CLOSE_EVENT 放入队尾
    closed_count = hub.close_session(mock_session)
    assert closed_count == 1

    # 让事件循环执行调度
    await asyncio.sleep(0)

    # 队列依然满，但队尾为 _CLOSE_EVENT
    assert q.qsize() == SSEHub._QUEUE_MAXSIZE

    items = []
    while not q.empty():
        items.append(q.get_nowait())

    # 第一项已被移出，最后一项必须是 _CLOSE_EVENT
    assert hub.is_close_event(items[-1])
    assert items[0]["msg"] == "data-1"


# ── FIX: P1-C3 —— 队列满时按消息类别分级丢弃（响应不可丢）─────────────


def _notification(seq):
    """通知类消息：有 method 无 id。"""
    return {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"seq": seq}}


def _response(rid):
    """响应类消息：有 id 无 method（客户端在等待匹配该 id）。"""
    return {"jsonrpc": "2.0", "id": rid, "result": {"ok": True}}


@pytest.mark.asyncio
async def test_is_response_classification(hub):
    """消息分类：带 id 无 method = 响应；带 method = 通知；close 控制事件 = 非响应。"""
    assert hub._is_response(_response(1)) is True
    assert hub._is_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32600}}) is True
    assert hub._is_response(_notification(1)) is False
    # server→client 请求（id + method）按通知类处理（当前服务端不发请求）
    assert hub._is_response({"jsonrpc": "2.0", "id": 1, "method": "x"}) is False
    assert hub._is_response(_CLOSE_EVENT) is False
    assert hub._is_response("not-a-dict") is False


@pytest.mark.asyncio
async def test_response_never_dropped_while_notification_evictable(hub, mock_session):
    """队列满时发布响应：挤掉最旧通知腾位，全部在途响应保留。"""
    q = hub.subscribe(mock_session)

    # 前半通知、后半响应，填满 256
    for i in range(128):
        q.put_nowait(_notification(i))
    for i in range(128):
        q.put_nowait(_response(i))
    assert q.full()

    hub.publish(mock_session, _response(999))
    await asyncio.sleep(0)  # 执行 call_soon_threadsafe 回调

    assert q.qsize() == SSEHub._QUEUE_MAXSIZE
    items = []
    while not q.empty():
        items.append(q.get_nowait())

    # 通知被挤掉 1 条（最旧的通知 0），其余通知全保留
    notifications = [m for m in items if hub._is_response(m) is False]
    assert len(notifications) == 127
    assert all(m["params"]["seq"] != 0 for m in notifications)
    # 全部 128 条在途响应 + 新响应 999 都在
    responses = [m for m in items if hub._is_response(m)]
    assert len(responses) == 129
    assert items[-1]["id"] == 999


@pytest.mark.asyncio
async def test_notification_dropped_when_queue_full_of_responses(hub, mock_session):
    """队列全为在途响应时发布通知：丢本条通知，不丢任何响应。"""
    q = hub.subscribe(mock_session)

    for i in range(SSEHub._QUEUE_MAXSIZE):
        q.put_nowait(_response(i))
    assert q.full()

    hub.publish(mock_session, _notification(42))
    await asyncio.sleep(0)

    # 通知未入队，响应一条不少
    assert q.qsize() == SSEHub._QUEUE_MAXSIZE
    ids = set()
    while not q.empty():
        ids.add(q.get_nowait()["id"])
    assert len(ids) == SSEHub._QUEUE_MAXSIZE  # 0..255 全部响应保留


@pytest.mark.asyncio
async def test_response_dropped_only_when_queue_all_responses(hub, mock_session):
    """队列全为在途响应时发布响应：丢弃最旧响应（记 error），新响应入队。"""
    q = hub.subscribe(mock_session)

    for i in range(SSEHub._QUEUE_MAXSIZE):
        q.put_nowait(_response(i))
    assert q.full()

    hub.publish(mock_session, _response(999))
    await asyncio.sleep(0)

    assert q.qsize() == SSEHub._QUEUE_MAXSIZE
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    # 最旧响应（id=0）被挤掉，其余响应 + 新响应 999 在
    ids = {m["id"] for m in items}
    assert 0 not in ids
    assert 999 in ids
    assert len(items) == SSEHub._QUEUE_MAXSIZE


@pytest.mark.asyncio
async def test_close_event_always_delivered_when_full(hub, mock_session):
    """队列满（无论内容）时 close 事件必须送达（P3-11 保证不回归）。"""
    q = hub.subscribe(mock_session)

    for i in range(SSEHub._QUEUE_MAXSIZE):
        q.put_nowait(_response(i))
    assert q.full()

    closed = hub.close_session(mock_session)
    assert closed == 1
    await asyncio.sleep(0)

    # 队列仍满（挤掉一条最旧响应），队尾为 close 事件
    assert q.qsize() == SSEHub._QUEUE_MAXSIZE
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert hub.is_close_event(items[-1])
    assert items[0]["id"] == 1  # 最旧响应 id=0 被挤掉

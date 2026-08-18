"""P3-9 回归测试：_query_with_retry 重连后返回最新连接，调用方正确归还。

旧实现只返回 rows，重连换新连接后调用方 finally 仍归还旧 conn：
- 新连接泄漏（从池取出但永不归还）
- 旧连接被重复归还（已 close 的连接再 putconn）
本测试用 Fake 池/连接模拟断线重连，验证修复后返回 (rows, conn) 且归还的是新连接。
"""

import pytest
import psycopg2

import app.runtime.core.storage.pg_store as pg_store


class FakeCursor:
    def __init__(self, fail_times: int = 0, rows=None):
        self.fail_times = fail_times
        self.rows = rows or [(1, "ok")]

    def execute(self, sql, params=()):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        return None

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    """模拟 psycopg2 连接：cursor 可配置首次失败次数。"""

    _counter = 0

    def __init__(self, fail_times: int = 0):
        FakeConn._counter += 1
        self.id = f"conn-{FakeConn._counter}"
        self.closed = False
        self.fail_times = fail_times
        self.putconn_calls = 0

    def cursor(self):
        return FakeCursor(fail_times=self.fail_times)

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class FakePool:
    """模拟连接池：记录 putconn 的调用与 close 标记。"""

    def __init__(self):
        self.putconn_conns = []  # [(conn, close), ...]

    def putconn(self, conn, close=False):
        conn.putconn_calls += 1
        self.putconn_conns.append((conn, close))
        if close:
            conn.closed = True


@pytest.fixture
def fake_pool(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(pg_store, "_get_pool", lambda: pool)
    monkeypatch.setattr(pg_store, "_get_pg_circuit_breaker", lambda: None)
    return pool


def _patch_reconnect(monkeypatch, reconnect_conns):
    """让 _get_conn 在重连时按序返回 reconnect_conns。"""
    it = iter(reconnect_conns)
    monkeypatch.setattr(pg_store, "_get_conn", lambda: next(it))


# ---------------------------------------------------------------------------
# 重连行为
# ---------------------------------------------------------------------------


def test_query_with_retry_returns_latest_conn_after_reconnect(monkeypatch, fake_pool):
    """断线重连后：返回 (rows, conn)，conn 是新连接，旧连接被关闭归还。"""
    old = FakeConn(fail_times=1)  # 首次查询抛 OperationalError
    new = FakeConn(fail_times=0)  # 重试成功
    _patch_reconnect(monkeypatch, [new])

    rows, conn = pg_store._query_with_retry(old, "SELECT 1")

    # 返回新连接（不是初始旧连接）
    assert conn is new
    assert conn is not old
    assert rows == [(1, "ok")]
    # 旧连接被 putconn(close=True) 关闭
    assert old.closed is True
    assert (old, True) in fake_pool.putconn_conns
    # 新连接尚未归还（由调用方归还）
    assert new.putconn_calls == 0


def test_query_with_retry_no_reconnect_returns_same_conn(monkeypatch, fake_pool):
    """无断线时：返回原连接，不触发 putconn。"""
    conn = FakeConn(fail_times=0)

    rows, returned = pg_store._query_with_retry(conn, "SELECT 1")

    assert returned is conn
    assert rows == [(1, "ok")]
    assert conn.putconn_calls == 0
    assert fake_pool.putconn_conns == []


def test_query_with_retry_fetch_all_false_returns_single_row(monkeypatch, fake_pool):
    """fetch_all=False 返回单行且携带最新连接。"""
    conn = FakeConn(fail_times=0)

    row, returned = pg_store._query_with_retry(conn, "SELECT 1", fetch_all=False)

    assert returned is conn
    assert row == (1, "ok")


def test_query_with_retry_raises_after_exhausting_retries(monkeypatch, fake_pool):
    """重试耗尽后：抛出原始 OperationalError，所有尝试过的连接均已关闭。"""
    old = FakeConn(fail_times=3)  # 首次就失败
    new1 = FakeConn(fail_times=2)  # 重连后仍失败
    new2 = FakeConn(fail_times=2)  # 再重连后仍失败 → 耗尽
    _patch_reconnect(monkeypatch, [new1, new2])

    with pytest.raises(psycopg2.OperationalError):
        pg_store._query_with_retry(old, "SELECT 1")

    assert all(c.closed for c in (old, new1, new2))
    assert len(fake_pool.putconn_conns) == 3


# ---------------------------------------------------------------------------
# 调用方归还行为（修复的核心：归还最新连接）
# ---------------------------------------------------------------------------


def test_store_caller_returns_latest_conn_to_pool(monkeypatch, fake_pool):
    """模拟 store 方法体：重连后 finally 归还的是新连接，而非旧连接。"""
    old = FakeConn(fail_times=1)
    new = FakeConn(fail_times=0)
    _patch_reconnect(monkeypatch, [new])

    # 复刻 store.get_entries 的调用模式（含 _put 归还最新 conn）
    conn = old
    try:
        rows, conn = pg_store._query_with_retry(
            conn, "SELECT timestamp, step, data FROM traces WHERE request_id = %s", ("r1",)
        )
        result = len(rows)
    finally:
        if conn is not None and not conn.closed:
            fake_pool.putconn(conn)

    assert result == 1
    # 归还的是新连接 new，而不是旧连接 old
    assert (new, False) in fake_pool.putconn_conns
    assert (old, False) not in fake_pool.putconn_conns
    # 旧连接在重连时已被 close 归还
    assert old.closed is True

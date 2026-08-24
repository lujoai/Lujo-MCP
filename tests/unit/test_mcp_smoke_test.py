"""单元测试：scripts/mcp_smoke_test.py 的 stdio 响应读取超时与 EOF 哨兵（死锁兜底）。"""
import queue

import pytest

from scripts import mcp_smoke_test as sm


class _FakeStdin:
    def write(self, _s: str) -> None:
        pass

    def flush(self) -> None:
        pass


class _FakeProc:
    stdin = _FakeStdin()


def test_send_times_out_when_no_response(monkeypatch):
    """服务端挂死（无任何响应）时 _send 应在超时后抛 TimeoutError，而非永久阻塞。"""
    monkeypatch.setattr(sm, "_ID", 0)
    monkeypatch.setattr(sm, "_READ_TIMEOUT", 0.05)
    out_q = queue.Queue()  # 永不收到响应

    with pytest.raises(TimeoutError):
        sm._send(_FakeProc(), out_q, "initialize", {})


def test_send_matches_by_id_and_skips_noise(monkeypatch):
    """_send 应按 id 匹配响应，跳过不匹配的 id 与非法 JSON 行。"""
    monkeypatch.setattr(sm, "_ID", 0)
    out_q = queue.Queue()
    out_q.put('{"jsonrpc":"2.0","id":999,"result":{}}')  # id 不匹配
    out_q.put("not-json")                                  # 非法 JSON
    out_q.put('{"jsonrpc":"2.0","id":1,"result":{"ok":true}}')  # 匹配

    resp = sm._send(_FakeProc(), out_q, "tools/list", {})
    assert resp["id"] == 1
    assert resp["result"]["ok"] is True


def test_start_readers_pushes_lines_then_eof_sentinel():
    """stdout 行入队，EOF 时推入 None 哨兵（供 _send 识别流提前关闭）。"""
    class _FakeStdout:
        def __init__(self) -> None:
            self._lines = ['{"id":1}\n']

        def readline(self) -> str:
            return self._lines.pop(0) if self._lines else ""

    class _FakeStderr:
        def readline(self) -> str:
            return ""

    proc = type("P", (), {"stdout": _FakeStdout(), "stderr": _FakeStderr()})()
    out_q = sm._start_readers(proc)

    assert out_q.get(timeout=1) == '{"id":1}\n'
    assert out_q.get(timeout=1) is None

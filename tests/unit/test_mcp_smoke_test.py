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


def test_wait_http_accepts_successful_health_payload(monkeypatch):
    """统一 transport 冒烟的 HTTP 检查应解析 2xx health JSON。"""
    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"degraded"}'

    monkeypatch.setattr(sm.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    assert sm._wait_http("http://127.0.0.1:8000/health", timeout=0.1) == {"status": "degraded"}


def test_wait_http_times_out_after_specified_timeout(monkeypatch):
    """_wait_http 必须尊重传入的 timeout 参数，不依赖全局 _READ_TIMEOUT。

    GitHub Actions Windows runner 上 PyInstaller 冻结二进制 --http 模式冷启动
    可能超过 10 秒（默认 _READ_TIMEOUT），_wait_http 必须支持独立超时控制。
    """
    call_count = 0

    def _always_timeout(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise ConnectionRefusedError("no server")

    monkeypatch.setattr(sm.urllib.request, "urlopen", _always_timeout)
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)  # 加速

    with pytest.raises(TimeoutError, match=r"0\.3s"):
        sm._wait_http("http://127.0.0.1:9999/health", timeout=0.3)

    # 确保它确实在重试（多次调用 urlopen），而不是第一次就放弃
    assert call_count > 1


def test_wait_http_uses_independent_timeout_not_global_read_timeout(monkeypatch):
    """_wait_http 的 timeout 必须独立于 _READ_TIMEOUT。

    即使 _READ_TIMEOUT 很短（如 0.01s），_wait_http 传入更大的 timeout
    时仍应继续重试直到自己的 deadline。
    """
    monkeypatch.setattr(sm, "_READ_TIMEOUT", 0.01)
    call_count = 0

    def _always_fail(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise ConnectionRefusedError("no server")

    monkeypatch.setattr(sm.urllib.request, "urlopen", _always_fail)
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)

    with pytest.raises(TimeoutError, match=r"1\.0s"):
        sm._wait_http("http://127.0.0.1:9999/health", timeout=1.0)

    # 如果它错误地用了 _READ_TIMEOUT (0.01s)，call_count 会很小
    # 用 1.0s timeout 时应该有更多重试
    assert call_count > 5


def test_parse_tool_arguments_requires_json_object():
    assert sm._parse_tool_arguments('{"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        sm._parse_tool_arguments("[]")
    with pytest.raises(ValueError):
        sm._parse_tool_arguments("not-json")

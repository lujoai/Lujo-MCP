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


def test_wait_http_accepts_html_response(monkeypatch):
    """统一 transport 冒烟的 /demo 检查应正常接收 HTML 响应而不因 json 解析崩溃。"""
    class _Headers:
        def get(self, key, default=""):
            if key.lower() == "content-type":
                return "text/html; charset=utf-8"
            return default

    class _Response:
        status = 200
        headers = _Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'<!DOCTYPE html><html><head><title>AI Debug Network Capture Demo</title></head><body><div id="ingestion-status"></div></body></html>'

    monkeypatch.setattr(sm.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    res = sm._wait_http("http://127.0.0.1:8130/demo", timeout=0.1)
    assert res["status"] == 200
    assert "text/html" in res["content_type"]
    assert res["length"] > 0


def test_normalize_http_urls():
    """验证 URL 列表支持多种传入格式（单串、列表、逗号分隔、多参数并存、去重）。"""
    assert sm._normalize_http_urls(None, None) == []
    assert sm._normalize_http_urls("http://127.0.0.1:8130/health") == ["http://127.0.0.1:8130/health"]
    assert sm._normalize_http_urls(
        ["http://127.0.0.1:8130/health", "http://127.0.0.1:8130/demo"]
    ) == ["http://127.0.0.1:8130/health", "http://127.0.0.1:8130/demo"]
    assert sm._normalize_http_urls(
        "http://127.0.0.1:8130/health,http://127.0.0.1:8130/demo"
    ) == ["http://127.0.0.1:8130/health", "http://127.0.0.1:8130/demo"]
    assert sm._normalize_http_urls(
        http_url=["http://127.0.0.1:8130/health"],
        http_probe_urls=["http://127.0.0.1:8130/demo", "http://127.0.0.1:8130/health"],
    ) == ["http://127.0.0.1:8130/health", "http://127.0.0.1:8130/demo"]


def test_validate_http_response_health():
    """/health 必须验证合法 JSON 且 status in ('ok', 'degraded')。"""
    # 合法状态 ok 与 degraded 均可通过
    assert sm._validate_http_response("http://127.0.0.1:8130/health", 200, "application/json", '{"status":"ok"}') == {"status": "ok"}
    assert sm._validate_http_response("http://127.0.0.1:8130/health", 200, "application/json", '{"status":"degraded"}') == {"status": "degraded"}

    # 非法 status 值抛 ValueError
    with pytest.raises(ValueError, match="status 异常.*unhealthy"):
        sm._validate_http_response("http://127.0.0.1:8130/health", 200, "application/json", '{"status":"unhealthy"}')
    with pytest.raises(ValueError, match="status 异常.*unknown"):
        sm._validate_http_response("http://127.0.0.1:8130/health", 200, "application/json", '{"status":"unknown"}')
    with pytest.raises(ValueError, match="status 异常.*None"):
        sm._validate_http_response("http://127.0.0.1:8130/health", 200, "application/json", '{"foo":"bar"}')

    # 非法 JSON 抛 ValueError
    with pytest.raises(ValueError, match="不是合法的 JSON"):
        sm._validate_http_response("http://127.0.0.1:8130/health", 200, "text/plain", "Internal Server Error")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        sm._validate_http_response("http://127.0.0.1:8130/health", 200, "application/json", '["ok"]')


def test_validate_http_response_demo():
    """/demo 必须验证 text/html Content-Type 与独有、稳定的页面标识内容。"""
    # 独有标志性内容（三者之一）均可通过
    for marker in ("AI Debug Network Capture Demo", "ingestion-status", "testXhrGet"):
        res = sm._validate_http_response(
            "http://127.0.0.1:8130/demo",
            200,
            "text/html; charset=utf-8",
            f"<!DOCTYPE html><html><body><div>{marker}</div></body></html>",
        )
        assert res["status"] == 200
        assert "text/html" in res["content_type"]

    # 包含普通 "Demo" 但不含独有标记的 HTML 页面必须抛错拒绝
    with pytest.raises(ValueError, match="缺少网络捕获演示页独有标识"):
        sm._validate_http_response(
            "http://127.0.0.1:8130/demo",
            200,
            "text/html; charset=utf-8",
            "<!DOCTYPE html><html><head><title>Demo Page</title></head><body><h1>Demo</h1></body></html>",
        )

    # 包含通用的 "demo" / "lujo" 词汇但无独有特征的页面同样拒绝
    with pytest.raises(ValueError, match="缺少网络捕获演示页独有标识"):
        sm._validate_http_response(
            "http://127.0.0.1:8130/demo",
            200,
            "text/html",
            "<html><body>Welcome to Lujo demo</body></html>",
        )

    # 常见 Web 服务器默认页拒绝
    with pytest.raises(ValueError, match="缺少网络捕获演示页独有标识"):
        sm._validate_http_response(
            "http://127.0.0.1:8130/demo",
            200,
            "text/html; charset=utf-8",
            "<!DOCTYPE html><html><body><h1>Welcome to Nginx</h1></body></html>",
        )

    # Content-Type 非 text/html 抛 ValueError
    with pytest.raises(ValueError, match="Content-Type 必须包含 text/html"):
        sm._validate_http_response(
            "http://127.0.0.1:8130/demo",
            200,
            "text/plain",
            "AI Debug Network Capture Demo",
        )
    with pytest.raises(ValueError, match="Content-Type 必须包含 text/html"):
        sm._validate_http_response(
            "http://127.0.0.1:8130/demo",
            200,
            "application/json",
            '{"status": "ok", "demo": true}',
        )


def test_validate_http_response_status_code_check():
    """非 2xx 状态码必须抛 ValueError。"""
    with pytest.raises(ValueError, match="不在 2xx 成功范围内"):
        sm._validate_http_response("http://127.0.0.1:8130/health", 500, "application/json", '{"status":"ok"}')
    with pytest.raises(ValueError, match="不在 2xx 成功范围内"):
        sm._validate_http_response("http://127.0.0.1:8130/demo", 404, "text/html", "Demo not found")


def test_cleanup_process_escalates_to_terminate_and_kill():
    """测试生产清理函数 _cleanup_process 的阶梯收口：wait 超时触发 terminate，再超时触发 kill。"""
    import subprocess

    class _MockProcess:
        def __init__(self):
            self.stdin = type("Stdin", (), {"closed": False, "close": lambda self: None})()
            self.terminate_called = False
            self.kill_called = False
            self._wait_calls = 0

        def wait(self, timeout=None):
            self._wait_calls += 1
            if self._wait_calls <= 2:
                raise subprocess.TimeoutExpired(cmd="test", timeout=timeout)
            return 0

        def terminate(self):
            self.terminate_called = True

        def kill(self):
            self.kill_called = True

    mock_proc = _MockProcess()
    # 实际调用生产代码的 _cleanup_process
    sm._cleanup_process(mock_proc)

    assert mock_proc.terminate_called is True
    assert mock_proc.kill_called is True


def test_cleanup_process_graceful_exit():
    """生产清理函数在子进程正常自退时优先关闭 stdin 并等待，不调用 terminate 或 kill。"""
    class _MockProcess:
        def __init__(self):
            self.closed_stdin = False
            self.terminate_called = False
            self.kill_called = False

            def _close(_stdin_self):
                self.closed_stdin = True

            self.stdin = type("Stdin", (), {"closed": False, "close": _close})()

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminate_called = True

        def kill(self):
            self.kill_called = True

    mock_proc = _MockProcess()
    sm._cleanup_process(mock_proc)

    assert mock_proc.closed_stdin is True
    assert mock_proc.terminate_called is False
    assert mock_proc.kill_called is False


def test_run_smoke_calls_cleanup_process_in_finally(monkeypatch):
    """验证 _run_smoke 在异常发生时通过真实 finally 路径调用生产清理函数 _cleanup_process。"""
    real_cleanup = sm._cleanup_process
    cleanup_called = False

    def _spy_cleanup(proc):
        nonlocal cleanup_called
        cleanup_called = True
        real_cleanup(proc)

    monkeypatch.setattr(sm, "_cleanup_process", _spy_cleanup)
    monkeypatch.setattr(sm, "_resolve_cmd", lambda _cmd: ["dummy"])

    mock_stdin = type("Stdin", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    mock_proc = type("MockPopen", (), {
        "stdin": mock_stdin,
        "stdout": type("Stdout", (), {"readline": lambda: ""})(),
        "stderr": type("Stderr", (), {"readline": lambda: ""})(),
        "wait": lambda self, timeout=None: 0,
        "terminate": lambda self: None,
        "kill": lambda self: None,
    })()
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *args, **kwargs: mock_proc)
    monkeypatch.setattr(sm, "_start_readers", lambda _proc: queue.Queue())

    def _fail_send(*_args, **_kwargs):
        raise RuntimeError("simulated protocol error")

    monkeypatch.setattr(sm, "_send", _fail_send)

    with pytest.raises(RuntimeError, match="simulated protocol error"):
        sm._run_smoke(tool=None, cmd="dummy")

    assert cleanup_called is True
    # 验证生产清理函数 _cleanup_process 实际执行并关闭了 stdin
    assert mock_stdin.closed is True


def test_run_smoke_fails_and_cleans_up_when_http_probe_fails(monkeypatch):
    """验证 HTTP 探针失败时 _run_smoke 打印错误返回 1 并通过 finally 执行生产清理函数 _cleanup_process。"""
    real_cleanup = sm._cleanup_process
    cleanup_called = False

    def _spy_cleanup(proc):
        nonlocal cleanup_called
        cleanup_called = True
        real_cleanup(proc)

    monkeypatch.setattr(sm, "_cleanup_process", _spy_cleanup)
    monkeypatch.setattr(sm, "_resolve_cmd", lambda _cmd: ["dummy"])

    mock_stdin = type("Stdin", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    mock_proc = type("MockPopen", (), {
        "stdin": mock_stdin,
        "stdout": type("Stdout", (), {"readline": lambda: ""})(),
        "stderr": type("Stderr", (), {"readline": lambda: ""})(),
        "wait": lambda self, timeout=None: 0,
        "terminate": lambda self: None,
        "kill": lambda self: None,
    })()
    monkeypatch.setattr(sm.subprocess, "Popen", lambda *args, **kwargs: mock_proc)
    monkeypatch.setattr(sm, "_start_readers", lambda _proc: queue.Queue())

    def _fail_wait_http(url, timeout):
        raise TimeoutError(f"HTTP probe timeout: {url}")

    monkeypatch.setattr(sm, "_wait_http", _fail_wait_http)

    rc = sm._run_smoke(tool=None, cmd="dummy", http_url="http://127.0.0.1:8130/health")
    assert rc == 1
    assert cleanup_called is True
    assert mock_stdin.closed is True


def test_run_smoke_direct_finally_invokes_production_cleanup(monkeypatch):
    """验证 _run_smoke 不 mock _cleanup_process 时，完全通过原生 finally 调用真实生产清理逻辑。"""
    monkeypatch.setattr(sm, "_resolve_cmd", lambda _cmd: ["dummy"])

    closed_stdin = False
    waited = False

    class _TrackingStdin:
        closed = False

        def close(self):
            nonlocal closed_stdin
            closed_stdin = True
            self.closed = True

    class _TrackingPopen:
        stdin = _TrackingStdin()
        stdout = type("Stdout", (), {"readline": lambda: ""})()
        stderr = type("Stderr", (), {"readline": lambda: ""})()

        def wait(self, timeout=None):
            nonlocal waited
            waited = True
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(sm.subprocess, "Popen", lambda *args, **kwargs: _TrackingPopen())
    monkeypatch.setattr(sm, "_start_readers", lambda _proc: queue.Queue())
    monkeypatch.setattr(sm, "_send", lambda *_args, **_kwargs: {"error": "handshake failed"})

    rc = sm._run_smoke(tool=None, cmd="dummy")
    assert rc == 1
    assert closed_stdin is True
    assert waited is True

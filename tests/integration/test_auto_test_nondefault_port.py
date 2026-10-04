"""工单（自定义端口传递）集成回归：非默认 CLI 端口下 auto_test 必须回传到本实例。

真实链路（非替身、非同进程 monkeypatch）：
1. 子进程启动真实服务：python -m app.mcp_server --http --http-port P
   （隔离 env：memory 存储、KB 关闭、免鉴权；显式剔除 HTTP_PORT/HOST/PORT，
   避免 env 掩盖 CLI 参数）；
2. 页面由本用例自带的 HTTP 服务提供：页面读取注入脚本的 script src（即 SDK
   endpoint）并经 console.error 回报，同时留一个唯一 marker；
3. 经 HTTP /mcp 调 auto_test（heavy 子进程执行）→ 主进程排水入库；
4. diagnose_issue 回查 marker，并断言回报的 endpoint 端口就是 P。

红相（修复前）：heavy worker 读不到父进程 CLI 解析出的端口 → endpoint 指向默认
8710，第 4 步断言失败（marker 仍可查到，因为私有 context 的拦截路由恰好也指向
8710；本轮修复消除的正是"endpoint 不是本实例"这一契约缺口与逃逸面）。

环境门禁：Playwright 或可用浏览器通道缺失时按环境缺失惯例 skip。
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright", reason="Playwright 不可用，跳过真实服务+浏览器链路")

from app.runtime.verifier.browser_launcher import resolve_launch_kwargs  # noqa: E402

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SERVICE_READY_TIMEOUT_S = 45.0
_TOOL_TIMEOUT_S = 180.0

PAGES: dict[str, str] = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _rand8() -> str:
    return format(random.getrandbits(32), "08x")


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path, "<!doctype html><body>missing</body>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def page_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture()
def service(tmp_path):
    """真实服务子进程：--http --http-port P（非默认端口），隔离 memory 实例。"""
    port = _free_port()
    assert port != 8710, "构造前提：必须是非默认端口"
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "memory"
    env["KB_PERSIST_ENABLED"] = "false"
    env["API_KEY"] = ""  # 免鉴权隔离实例：不使用开发者 .env 里的真实 key
    env["PYTHONIOENCODING"] = "utf-8"
    for stale in ("HTTP_PORT", "HTTP_HOST", "HOST", "PORT"):
        env.pop(stale, None)  # 关键：env 不得掩盖 CLI --http-port

    log_path = tmp_path / "service.log"
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp_server", "--http", "--http-port", str(port)],
        cwd=str(_REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,  # 保持打开：stdio 侧 EOF 会触发统一模式关闭
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + _SERVICE_READY_TIMEOUT_S
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)
    if not ready:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
        log_f.close()
        text = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        pytest.fail(f"服务未就绪（port={port}，exit={proc.poll()}）：{text}")
    try:
        yield port
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log_f.close()


def _mcp_post(port: int, payload: dict, session: str | None = None, timeout: float = 60.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    if session:
        req.add_header("Mcp-Session-Id", session)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
        return r.status, r.headers.get("Mcp-Session-Id"), (json.loads(raw) if raw.strip() else None)


def _mcp_call(port: int, session: str, name: str, arguments: dict, timeout: float = _TOOL_TIMEOUT_S) -> dict:
    _status, _sess, body = _mcp_post(
        port,
        {"jsonrpc": "2.0", "id": random.getrandbits(30), "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
        session=session, timeout=timeout,
    )
    content = (body or {}).get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else ""
    try:
        data = json.loads(text)
    except Exception:
        data = {"_raw_text": text}
    return data if isinstance(data, dict) else {"_raw": data}


def _diagnose_blob(port: int, session: str) -> str:
    """无参 diagnose → 逐候选 request_id 回查，拼成可搜索文本（查询面真实行为）。"""
    top = _mcp_call(port, session, "diagnose_issue", {"since_minutes": 0})
    texts = [json.dumps(top, ensure_ascii=False)]
    for cand in (top.get("candidates") or []):
        rid = cand.get("request_id")
        if not rid:
            continue
        texts.append(json.dumps(
            _mcp_call(port, session, "diagnose_issue", {"request_id": rid}),
            ensure_ascii=False,
        ))
    return "\n".join(texts)


def test_auto_test_endpoint_matches_nondefault_cli_port(service, page_server):
    if resolve_launch_kwargs() is None:
        pytest.skip("无可用浏览器通道（chromium/Chrome/Edge），跳过真实服务+浏览器链路")

    port = service
    marker = f"PORT-MARKER-{_rand8()}"
    page_path = f"/p-{_rand8()}"
    PAGES[page_path] = (
        "<!doctype html><html><head><title>port-probe</title></head><body>"
        "<button id='b'>ok</button><script>\n"
        f"console.error('{marker}');\n"
        "var tries = 0;\n"
        "var timer = setInterval(function () {\n"
        '  var s = document.querySelector(\'script[src*="ai-debug.js"]\');\n'
        "  if (s && s.src) { clearInterval(timer); console.error('LUJO-ENDPOINT-SEEN=' + s.src); }\n"
        "  else if (++tries > 250) { clearInterval(timer); console.error('LUJO-ENDPOINT-MISSING'); }\n"
        "}, 20);\n"
        "</script></body></html>"
    )
    url = f"http://127.0.0.1:{page_server}{page_path}"

    status, session, body = _mcp_post(port, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "nondefault-port-test", "version": "0.0.1"}},
    })
    assert status == 200 and body and "result" in body, f"initialize 失败: {status} {body}"
    session = session or (body.get("result") or {}).get("sessionId")
    assert session, "initialize 未返回 Mcp-Session-Id"
    _mcp_post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session)

    result = _mcp_call(port, session, "auto_test",
                       {"url": url, "max_actions": 1, "observe_ms": 2000})
    capture = result.get("sdk_capture") or {}
    assert capture.get("init") == "ready", f"SDK 未就绪：{result}"
    assert (capture.get("events_ingested") or 0) >= 1, f"事件未入本实例存储：{result}"
    assert not capture.get("route_failures"), f"父实例不应有拦截异常：{capture}"

    blob = _diagnose_blob(port, session)
    assert marker in blob, f"marker 未回查到（本实例存储/查询面）：{blob[-800:]}"

    expected = f"LUJO-ENDPOINT-SEEN=http://127.0.0.1:{port}/ai-debug.js"
    import re

    found = re.search(r"LUJO-ENDPOINT-SEEN=([^\\\"\s]+)", blob)
    observed = found.group(0) if found else "（未观察到注入脚本标签）"
    assert expected in blob, (
        f"注入的 SDK endpoint 未指向本实例端口 {port}（父进程 CLI 解析结果未传给 heavy worker）；"
        f"实际回报 {observed}"
    )
    assert "LUJO-ENDPOINT-MISSING" not in blob, "页面未观察到注入脚本标签"

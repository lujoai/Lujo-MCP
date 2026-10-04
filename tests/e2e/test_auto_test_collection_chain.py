"""auto_test 真实浏览器采集链路 e2e：页面 → 拦截回传 → 主实例入库 → diagnose 可查。

背景（v1.0.0 修复工作包，先失败测试）：
本用例不使用替身——真实 chromium 打开独立端口上的测试页面（页面零 SDK 接线、
服务器保持默认 CORS 收紧），auto_test 经 init script 注入 Browser SDK，SDK 上报
由 context.route 拦截进子进程事件队列，结果经 heavy IPC 回主进程排水入库，
随后 diagnose 候选枚举必须能回查到本次 marker。

覆盖断点组合（验收矩阵 B/C/D 的自动化守护）：
- 早期 console.error（SDK 加载前，验证 init script 缓冲回放）；
- 观察窗口内延迟 console.error（验证观察窗口与回传完成确认）；
- fetch 到无监听端口的真实连接失败（status_code=0 语义保持）。

环境门禁：Playwright + 可用浏览器通道缺失时按环境缺失惯例 skip。
"""
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright", reason="Playwright 不可用，跳过真实浏览器链路")

from app.config import settings  # noqa: E402
from app.runtime.verifier.browser_launcher import resolve_launch_kwargs  # noqa: E402


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _PageHandler(BaseHTTPRequestHandler):
    """提供单页：早期错误 + 延迟错误 + 真实连接失败，零 SDK 接线。"""

    marker_early = ""
    marker_delayed = ""
    delayed_ms = 1200
    dead_port = 1

    def do_GET(self):
        # 页面零 SDK 接线；三段行为：早期错误（SDK 加载前）、SDK ready 后的
        # 死端口 fetch（回环 connection-refused 在 Windows 上 ~2s 浮现，由
        # 观察窗口的在途请求清空条件覆盖）、观察窗口内延迟错误。
        # % 格式化：JS 花括号保持字面量，避免 f-string {{}} 折叠踩坑。
        page = (
            "<!doctype html><html><head><title>chain</title></head><body>"
            "<button id='b1'>ok</button>"
            "<script>\n"
            "console.error('%(early)s');\n"
            # 死端口 fetch 放在 SDK ready 之后：SDK fetch 钩子对安装前发出的
            # 在途请求不可观察（注入式采集的固有边界）；回环
            # connection-refused 在 Windows 上 ~2s 浮现，由观察窗口的在途
            # 请求清空条件覆盖。
            "setTimeout(function(){ fetch('http://127.0.0.1:%(dead)d/api/none')"
            ".catch(function() {}); }, 1500);\n"
            "setTimeout(function(){ console.error('%(delayed)s'); }, %(ms)d);\n"
            "</script></body></html>"
        ) % {
            "early": self.marker_early,
            "delayed": self.marker_delayed,
            "dead": self.dead_port,
            "ms": self.delayed_ms,
        }
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def marker_page_server():
    """独立端口的最小页面服务（与 Lujo e2e 服务器不同端口，模拟目标项目）。"""
    ts = int(time.time() * 1000)
    _PageHandler.marker_early = f"E2E-EARLY-CONSOLE chain-{ts}"
    _PageHandler.marker_delayed = f"E2E-DELAYED-CONSOLE chain-{ts}"
    # “确认无监听”的本机端口：绑定后立即释放即处于无监听态（本地回环实测稳定）
    _PageHandler.dead_port = _find_free_port()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}/page",
            "marker_early": _PageHandler.marker_early,
            "marker_delayed": _PageHandler.marker_delayed,
            "dead_port": _PageHandler.dead_port,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.asyncio
async def test_auto_test_real_browser_chain_into_diagnose(marker_page_server):
    """真实浏览器：auto_test 采集 → 排水入库 → diagnose 候选可回查 marker。"""
    if resolve_launch_kwargs() is None:
        pytest.skip("无可用浏览器通道（chromium/Chrome/Edge），跳过真实浏览器链路")

    from app.mcp.tools import auto_test_api
    from app.mcp.tools.diagnose_api import _enumerate_fault_candidates
    from app.mcp.tools.ingest_dispatch import drain_result_ingest_events
    from app.runtime.core import trace_repo

    # 服务器保持默认 CORS 收紧（不设 CORS_ORIGINS），页面零 SDK 接线
    monkey_settings = {
        "auto_inject_sdk": True,
        "http_host": settings.http_host,
        "http_port": settings.http_port,
    }
    saved = {k: getattr(settings, k) for k in monkey_settings}
    for k, v in monkey_settings.items():
        setattr(settings, k, v)
    try:
        result = await auto_test_api.auto_test_handler({
            "url": marker_page_server["url"],
            "max_actions": 2,
            "observe_ms": 5000,
        })
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)

    assert "error" not in result, result

    status = result.get("sdk_capture")
    assert isinstance(status, dict), f"缺少 sdk_capture：{result}"
    assert status.get("init") == "ready", status
    assert status.get("events_captured", 0) >= 2, status
    assert status.get("delivery") in ("complete", "timeout"), status

    # 主进程排水入库（heavy IPC 回来后的主进程侧动作）
    drain_result_ingest_events(result)
    assert status.get("events_ingested", 0) >= 2, status

    # diagnose 候选枚举可查，且 marker 可从存储回查
    candidates, _complete = _enumerate_fault_candidates(None, since_minutes=0)
    assert candidates, "入库后 diagnose 候选不应为空"

    # 检索面覆盖全部桶：SDK 会话桶在存在异常实体时是现场的别名桶
    # （消歧逻辑刻意不重复计数），只扫候选桶会漏掉 console/network 记录
    from app.mcp.tools.diagnose_api import _scan_bucket_ids

    console_texts: list[str] = []
    network_failed: list[dict] = []
    for bucket in _scan_bucket_ids()[0]:
        for entry in trace_repo.get_console_logs(bucket):
            console_texts.append(str(entry.get("message") or ""))
        for rec in trace_repo.get_network_records(bucket):
            status_code = rec.get("status_code", rec.get("status"))
            if status_code == 0 or rec.get("error"):
                network_failed.append(rec)

    assert any(marker_page_server["marker_early"] in t for t in console_texts), \
        f"早期 console marker 应可回查：{console_texts[:5]}"
    assert any(marker_page_server["marker_delayed"] in t for t in console_texts), \
        f"延迟 console marker 应可回查（观察窗口）：{console_texts[:5]}"
    assert any(
        f":{marker_page_server['dead_port']}/" in str(r.get("url") or "")
        for r in network_failed
    ), f"真实连接失败应以 status_code=0 语义入库：{network_failed[:3]}"

"""集成测试：Playwright 真实浏览器 UI 验证链路。

覆盖点：
- 真实启动 Chromium
- 访问本地 HTTP 页面
- 执行 click 交互
- 验证 DOM 变化
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest

from app.runtime.verifier import ui_runner


@pytest.fixture
def local_ui_site():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "index.html").write_text(
            """
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>ui verify live</title></head>
  <body>
    <button id="go" onclick="
      document.getElementById('result').className='ready';
      document.getElementById('result').textContent='done';
      window.location.hash='done';
    ">Go</button>
    <div id="result"></div>
  </body>
</html>
            """.strip(),
            encoding="utf-8",
        )

        handler = partial(SimpleHTTPRequestHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            yield f"http://{host}:{port}/index.html"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


@pytest.mark.integration
def test_run_ui_verification_against_live_local_page(monkeypatch, local_ui_site):
    if not ui_runner.is_available():
        pytest.skip("playwright 不可用，跳过真实浏览器 UI 验证")

    monkeypatch.setattr("app.config.settings.ui_url_allow_private", True)

    spec = {
        "kind": "ui",
        "target": local_ui_site,
        "expect": {
            "interactions": [
                {
                    "action": "click",
                    "selector": "#go",
                    "expect": {
                        "state_change": {"dom_change": "#result.ready"},
                        "assertions": [
                            {"type": "text", "selector": "#result", "equals": "done"},
                            {"type": "url", "contains": "#done"},
                        ],
                    },
                }
            ]
        },
    }

    result = ui_runner.run_ui_verification(spec, timeout_ms=10000)

    assert result["matched"] is True
    assert result["silent_failure"] is False
    assert result["diffs"] == []
    assert result["security"]["target"]["allowed"] is True
    assert result["security"]["target"]["rule"] == "allow_private"
    interaction = result["interactions"][0]
    assert [item["type"] for item in interaction["assertions"]] == [
        "dom_change",
        "text",
        "url",
    ]
    assert all(item["matched"] is True for item in interaction["assertions"])

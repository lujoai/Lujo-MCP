"""W3-5 · U09 真实 Playwright 进程树取证（不 mock；Windows + Job 后端）。

对应 DESIGN_C3 §5 / CHECKLIST W3-5：
- 真实 auto_test（async heavy）经生产终止链（降级链 Job + 三级终止）执行，
  超时在浏览器运行中触发 → 记录 worker PID、浏览器进程集、终止耗时；
- chromium 残留以**全量进程快照差集**取证（before/during/after），不以
  「未看到」销案；
- ``chromium user-data-dir`` 锁：本链路 chromium 为非持久化（headless 无
  ``--user-data-dir``），锁核对如实记录为 n/a。

取证输出：``outputs/c_batch/evidence/w3_5/u09_evidence.json``（可复现命令 =
本文件的 pytest 运行）。
"""

from __future__ import annotations

import http.server
import json
import os
import pickle
import subprocess
import sys
import threading
import time

import pytest

from app.mcp.protocol.termination import backend as backend_mod
from app.mcp.protocol.termination._win32 import process_exists

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="U09 取证绑定 Windows Job 后端（POSIX skip：环境理由）",
    ),
]

_EVIDENCE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "outputs", "c_batch", "evidence", "w3_5"
)

_BROWSER_IMAGES = {
    "chrome.exe", "chrome-headless-shell.exe", "chromedriver.exe", "headless_shell.exe",
}


def _snapshot_browser_pids() -> list[int]:
    """全量浏览器进程快照（tasklist 全量 dump，单次 ~0.3s，远快于 CIM）。"""
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=30,
    ).stdout
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() in _BROWSER_IMAGES:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def _page_server():
    """本地静态页（同一样本内一致；auto_test 的 SSRF 守卫需回环放行）。"""
    html = (
        b"<!doctype html><html><body><h1>u09</h1>"
        b"<button id=b>x</button><span id=status>ready</span></body></html>"
    )

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            time.sleep(0.4)  # 扩大浏览器存活窗口（快照可采样）
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_u09_real_playwright_tree_reclaim(tmp_path):
    """U09：真实 auto_test（chromium headless）超时终止 → 整树回收取证。

    样本 N=3；取证 JSON 落 outputs/c_batch/evidence/w3_5/。定级纪律：
    残留以快照差集为准，不定级为「未看到」。
    """
    from app.runtime.verifier import ui_runner

    if not ui_runner.is_available():
        pytest.skip("playwright 未安装（环境理由）")

    baseline = _snapshot_browser_pids()  # 运行前基线（本机可能已有无关 chrome）
    os.environ.setdefault("UI_URL_ALLOW_PRIVATE", "true")
    samples: list[dict] = []

    try:
        for i in range(3):
            server = _page_server()
            port = server.server_address[1]
            arguments = {
                "url": f"http://127.0.0.1:{port}/",
                "max_actions": 2,
                "capture_console": True,
                "capture_network": True,
            }
            request_bytes = pickle.dumps(arguments, protocol=pickle.HIGHEST_PROTOCOL)

            import app.mcp.protocol.heavy_spawn as hs
            from app.mcp.protocol.heavy_spawn import GO_BYTE

            # 生产链路：降级链 spawn（Job 收编）→ 握手 → go 后业务
            attempt, decision = backend_mod.spawn_with_backend(0, [
                sys.executable, "-m", "app.mcp.protocol.heavy_worker_entry",
                "--lujo-heavy-worker", "app.mcp.tools.auto_test_api",
                "auto_test_handler",
            ])
            worker_pid = attempt.proc.pid
            attempt.result.start_reader()
            attempt.write_request_async(request_bytes)

            rec: dict = {
                "sample": i,
                "worker_pid": worker_pid,
                "backend": decision.backend,
                "console_reachable": decision.console_reachable,
            }
            deadline_ready = time.monotonic() + 30
            while not attempt.result.ready.wait(timeout=0.05):
                assert time.monotonic() < deadline_ready, "ready 超时"
                assert attempt.proc.poll() is None, "worker 提前退出"
            stdin = attempt._stdin or attempt.proc.stdin  # noqa: SLF001 —— 测试控制 go 拍点
            stdin.write(GO_BYTE)
            stdin.flush()

            # 后台连续采样（0.15s 步长）：捕获业务窗口内的全部浏览器 PID
            stop = threading.Event()
            seen: set[int] = set()

            def _sampler():
                while not stop.is_set():
                    for pid in _snapshot_browser_pids():
                        if pid not in baseline:
                            seen.add(pid)
                    time.sleep(0.15)

            sampler = threading.Thread(target=_sampler, daemon=True)
            sampler.start()

            # 等采样器观察到浏览器 PID（业务确已开始、浏览器运行中）
            deadline_seen = time.monotonic() + 25
            while time.monotonic() < deadline_seen and not seen:
                time.sleep(0.05)
            rec["browser_pids_during"] = sorted(seen)

            # 浏览器运行中触发三级终止（计时）
            t_kill = time.monotonic()
            exitcode = backend_mod.terminate_attempt(attempt, decision, grace=5.0)
            rec["terminate_seconds"] = round(time.monotonic() - t_kill, 3)
            rec["worker_exitcode"] = exitcode
            rec["worker_gone"] = attempt.proc.poll() is not None

            # 树清空取证：1s 收尾拍 + 差集
            stop.set()
            sampler.join(timeout=10)
            time.sleep(1.0)
            after = [p for p in _snapshot_browser_pids() if p not in baseline]
            rec["browser_pids_during"] = sorted(seen)
            rec["browser_residual_after"] = after
            rec["tree_clear"] = rec["worker_gone"] and not after
            rec["browser_seen_during_business"] = bool(rec["browser_pids_during"])
            samples.append(rec)
            print(f"U09 sample {i}: backend={decision.backend} "
                  f"terminate={rec['terminate_seconds']}s residual={after}")
    finally:
        os.makedirs(_EVIDENCE_DIR, exist_ok=True)
        all_clear = bool(samples) and all(s["tree_clear"] for s in samples)
        evidence = {
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "n_samples": len(samples),
            "baseline_browser_pids": baseline,
            "user_data_dir_lock": (
                "n/a（chromium headless 无 --user-data-dir，非持久化 profile）"
            ),
            "samples": samples,
            "verdict": (
                "整树回收达标（Windows job 后端）" if all_clear else "存在残留 → 按定级纪律处置"
            ),
        }
        with open(os.path.join(_EVIDENCE_DIR, "u09_evidence.json"), "w", encoding="utf-8") as fh:
            json.dump(evidence, fh, ensure_ascii=False, indent=2)

    assert samples and all(s["tree_clear"] for s in samples), evidence["verdict"]
    assert all(s["browser_seen_during_business"] for s in samples), (
        "取证前提：终止时浏览器必须仍在运行（否则样本无效）"
    )
    assert all(s["backend"] in ("job", "breakaway+job", "direct-child") for s in samples)
    assert all(process_exists(s["worker_pid"]) is False for s in samples)

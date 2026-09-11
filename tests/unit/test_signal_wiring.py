"""W4-3（B15/C4）薄信号 handler 接线验收。

对应 DESIGN_C4 §1.1 与 CHECKLIST W4-3：
- 信号回调只发布退出意图和首次触发时间（shutdown.signal_handler_stub 语义：
  普通属性赋值，无锁/无日志/无清理）；
- uvicorn serve 路径经 handle_exit 包装适配（原语义保留，幂等防递归链），
  覆盖统一模式与独立 HTTP 两个实际运行入口；
- 清理不在信号回调内执行——交给可调度上下文（主循环 unwind / 控制路径）。
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from app.mcp.protocol import shutdown as sh


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(sh, "_intent_lock", threading.Lock())
    monkeypatch.setattr(sh, "_intent_t0", None)
    monkeypatch.setattr(sh, "_intent_reasons", frozenset())
    monkeypatch.setattr(sh, "_signal_t0", None)
    monkeypatch.setattr(sh, "_signal_reason", None)
    monkeypatch.setattr(sh, "_deadline", None)
    yield


def test_stdio_signal_handler_records_intent_then_exits():
    """stdio：回调先发布意图（t0/reason），再以 SystemExit 交主循环 unwind
    ——cleanup_resources 不在回调内执行（源码锁定）。"""
    import app.mcp_server as stdio

    source = inspect.getsource(stdio._signal_handler)
    assert "cleanup_resources" not in source, "清理不得在信号回调内执行"
    assert "signal_handler_stub" in source

    with pytest.raises(SystemExit):
        stdio._signal_handler(2, None)
    assert sh._signal_t0 is not None  # 意图已薄发布（正式记账由监督线程采纳）
    assert sh._signal_reason == "signal"


def test_wrap_uvicorn_handle_exit_records_intent_and_preserves_original():
    """uvicorn serve 路径适配：原 handle_exit 前发布意图；原语义保留。"""
    calls = []

    class _FakeServer:
        _intent_wrapped = False

        def handle_exit(self, signum, frame):
            calls.append(("original", signum))

    server = _FakeServer()
    sh.wrap_uvicorn_handle_exit(server)
    assert server._intent_wrapped is True
    server.handle_exit(15, None)
    assert calls == [("original", 15)]
    assert sh._signal_t0 is not None  # 意图已薄发布（正式记账由监督线程采纳）


def test_wrap_uvicorn_handle_exit_idempotent_no_recursion():
    """幂等防递归链：已包装的 server 再次包装被跳过（原 handler 只包一层）。"""
    calls = []

    class _FakeServer:
        _intent_wrapped = False

        def handle_exit(self, signum, frame):
            calls.append(("original", signum))

    server = _FakeServer()
    sh.wrap_uvicorn_handle_exit(server)
    sh.wrap_uvicorn_handle_exit(server)  # 第二次包装必须跳过
    server.handle_exit(2, None)
    assert calls == [("original", 2)]  # 仅一次原语义调用（无递归链）


def test_main_py_http_entry_uses_wrapped_serve():
    """独立 HTTP 入口：显式 Server + 包装 handle_exit（uvicorn.run 黑盒无法
    接线），且在 uvicorn.run/serve 之前完成监督者创建。"""
    import app.main as main_mod

    source = inspect.getsource(main_mod)
    assert "wrap_uvicorn_handle_exit" in source
    assert "ensure_exit_supervisor()" in source


def test_mcp_server_unified_mode_wraps_serve_path():
    """统一模式：uvicorn serve 路径包装覆盖实际运行入口。"""
    import app.mcp_server as stdio

    source = inspect.getsource(stdio)
    assert "wrap_uvicorn_handle_exit" in source


def test_watchdog_adopt_signal_publication(monkeypatch):
    """§1.3.2：信号回调只薄发布 → 监督线程独立识别截止时间（t0=信号时刻）并
    在 deadline 到点无阻塞强退。"""
    fired = []
    monkeypatch.setattr(sh, "EXIT_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(sh.os, "_exit", lambda code: fired.append((time.monotonic(), code)))

    sup = sh.ExitSupervisor.create()
    sh.signal_handler_stub(2, None)  # 仅薄发布（无正式记账）
    t_pub = sh._signal_t0
    assert sh.exit_intent_recorded() is False  # 正式记账未发生
    for _ in range(150):
        if fired:
            break
        time.sleep(0.02)
    assert fired, "监督线程未采纳信号薄发布"
    fired_at, code = fired[0]
    assert code == 0
    assert fired_at >= t_pub + 0.2 - 0.05  # deadline = 信号 t0 + 25s（此处 0.2s）
    sup.shutdown_watchdog()

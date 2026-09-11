"""W4-2（B15/C4）看门狗线程接线验收：独立入口接纳调用前创建并确认就绪。

对应 DESIGN_C4 §1.1 与 CHECKLIST W4-2：
- 单个 daemon 监督线程；**接纳调用前**创建并确认就绪，创建失败则**启动失败**；
- 只观察退出状态与期限，不执行工具、不复用业务进程、不构成常驻 worker 池；
- 内部异常不得静默吞掉（启动期失败 → 启动失败；已武装后 → 同一最后防线）。

接线覆盖三个独立入口：mcp_server.main（纯 stdio / 统一模式）、main.py
（独立 HTTP）。EOF 代理接在唯一 stdin 输入生产者上（stdio）。
"""

from __future__ import annotations

import inspect
import time

import pytest

from app.mcp.protocol import shutdown as sh


def test_ensure_exit_supervisor_is_idempotent(monkeypatch):
    """幂等：重复调用返回同一实例（单监督线程）。"""
    monkeypatch.setattr(sh, "_supervisor", None)
    created = []
    monkeypatch.setattr(
        sh.ExitSupervisor, "create",
        classmethod(lambda cls: created.append("s") or ("sentinel",)),
    )
    first = sh.ensure_exit_supervisor()
    second = sh.ensure_exit_supervisor()
    assert first is second
    assert len(created) == 1


def test_ensure_exit_supervisor_create_failure_propagates(monkeypatch):
    """创建失败 → 异常向上传播 = 启动失败（不得吞掉后继续接纳）。"""
    monkeypatch.setattr(sh, "_supervisor", None)
    def _boom(cls):
        raise RuntimeError("thread resources exhausted")

    monkeypatch.setattr(sh.ExitSupervisor, "create", classmethod(_boom))
    with pytest.raises(RuntimeError):
        sh.ensure_exit_supervisor()


def test_watchdog_thread_is_daemon_named_and_minimal():
    """监督线程：daemon、命名 lujo-exit-watchdog；只观察（无工具/池/日志
    接触——结构性源码扫描）。"""
    sup = sh.ExitSupervisor.create()
    try:
        thread = sup._thread
        assert thread is not None and thread.daemon is True
        assert thread.name == "lujo-exit-watchdog"
        assert thread.is_alive()
        source = inspect.getsource(sh.ExitSupervisor._watch)
        for forbidden in ("register_tool", "spawn_attempt", "terminate_attempt",
                          "handshake", "logger."):
            assert forbidden not in source, f"监督线程含越权调用: {forbidden}"
    finally:
        sup.shutdown_watchdog()


def test_stdio_entry_wires_eof_proxy_and_supervisor():
    """stdio/统一入口接线：main() 创建监督者；_run_stdio_transport 将 EOF
    代理接在 sys.stdin.buffer（唯一输入生产者）上。"""
    import app.mcp_server as stdio

    main_src = inspect.getsource(stdio.main)
    assert "ensure_exit_supervisor()" in main_src

    run_src = inspect.getsource(stdio._run_stdio_transport)
    assert "wrap_stdin_with_eof_awareness" in run_src
    assert "create_eof_aware_stdin_proxy" in inspect.getsource(shutdown_mod_mod := __import__("app.mcp.protocol.shutdown", fromlist=["x"]))
    assert 'record_exit_intent("stdio_eof")' in run_src
    # 代理接在 sys.stdin.buffer（唯一生产者），非下游 receive()
    assert "sys.stdin" in run_src


def test_unified_and_http_entries_create_supervisor():
    """统一模式与独立 HTTP 入口：接纳调用前创建监督者。"""
    import app.main as main_mod
    import app.mcp_server as stdio

    unified_src = inspect.getsource(stdio.main)
    assert "ensure_exit_supervisor()" in unified_src

    http_src = inspect.getsource(main_mod)
    assert "ensure_exit_supervisor()" in http_src
    # HTTP 入口在 serve 之前接线（独立入口接纳前）；uvicorn.run 已替换为
    # 显式 Server + handle_exit 包装
    assert "wrap_uvicorn_handle_exit" in http_src
    assert "uvicorn.run(" not in http_src
    assert http_src.index("ensure_exit_supervisor()") < http_src.index("http_server.run()")


def test_supervisor_record_arms_deadline_once(monkeypatch):
    """监督者 record：首记定 deadline，重复通知不刷新（与模块记账一致）。

    打补丁 os._exit：本用例真实武装了 deadline（25s），不打补丁会真的
    杀掉 pytest 进程（看门狗工作正常的副作用）。"""
    monkeypatch.setattr(sh.os, "_exit", lambda code: None)
    sup = sh.ExitSupervisor.create()
    try:
        t1 = sup.record("stdio_eof")
        time.sleep(0.05)
        sup.record("signal")
        assert sup.deadline == pytest.approx(t1 + sh.EXIT_DEADLINE_SECONDS, abs=0.01)
    finally:
        sup.shutdown_watchdog()

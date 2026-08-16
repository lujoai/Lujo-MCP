"""单元测试：stdio 传输 R3-4 修复（daemon 线程读 stdin，EOF 干净退出）"""
import asyncio
import io
import sys
import types

import pytest

from app.mcp.transports import stdio


def _isolate(monkeypatch):
    """隔离外部依赖：工具注册 / 日志重配 / app.mcp_server 清理函数。"""
    monkeypatch.setattr(stdio, "register_all_tools", lambda: None)
    monkeypatch.setattr(stdio, "_configure_stdio_logging", lambda: None)
    stub = types.ModuleType("app.mcp_server")
    stub.cleanup_resources = lambda: None
    monkeypatch.setitem(sys.modules, "app.mcp_server", stub)


@pytest.mark.asyncio
async def test_run_stdio_processes_request_and_exits_on_eof(monkeypatch, capsys):
    _isolate(monkeypatch)

    async def fake_dispatch(req):
        return {"jsonrpc": "2.0", "id": req.id, "result": {"echo": True}}

    monkeypatch.setattr(stdio, "dispatch", fake_dispatch)
    payload = '{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}'
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload + "\n"))

    # 若 stdin 阻塞读导致挂死，wait_for 会超时失败
    await asyncio.wait_for(stdio.run_stdio(), timeout=5)

    out = capsys.readouterr().out
    assert '"id": 1' in out
    assert "echo" in out


@pytest.mark.asyncio
async def test_run_stdio_skips_blank_lines_and_notifications(monkeypatch, capsys):
    _isolate(monkeypatch)

    async def fake_dispatch(req):
        return {"jsonrpc": "2.0", "id": req.id, "result": {}}

    monkeypatch.setattr(stdio, "dispatch", fake_dispatch)
    payload = '\n{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    await asyncio.wait_for(stdio.run_stdio(), timeout=5)

    # 空行跳过；通知（无 id）不写回响应
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_run_stdio_parse_error_returns_response_not_crash(monkeypatch, capsys):
    _isolate(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json\n"))

    await asyncio.wait_for(stdio.run_stdio(), timeout=5)

    out = capsys.readouterr().out
    assert "-32700" in out  # PARSE_ERROR 结构化返回，进程不崩溃

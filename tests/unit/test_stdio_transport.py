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


class _BufferedStdin:
    """模拟真实进程 stdin：文本流挂在底层 buffer 上（BytesIO）。"""

    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


@pytest.mark.asyncio
async def test_run_stdio_bad_utf8_frame_does_not_kill_service(monkeypatch, capsys):
    """FIX: v0.6.6 坏输入杀服务 —— 坏 UTF-8 字节帧不再被当 EOF。

    旧行为：sys.stdin(errors=strict) 的 readline() 遇坏字节抛 UnicodeDecodeError
    且此后流永久损坏（后续读取恒为空=EOF），reader 把异常一律按 EOF 处理，
    单条坏帧即让整个服务退出、后续合法请求全部失效。
    新行为：读底层 buffer 并以 errors="replace" 解码，坏帧回 PARSE_ERROR(-32700)
    后继续处理同连接的后续合法请求。
    """
    _isolate(monkeypatch)

    async def fake_dispatch(req):
        return {"jsonrpc": "2.0", "id": req.id, "result": {"ok": True}}

    monkeypatch.setattr(stdio, "dispatch", fake_dispatch)

    frames = (
        b'\xff\xfe{"method":"ping"}\n'                                      # 坏字节帧
        b'{"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}\n'    # 合法帧
    )
    monkeypatch.setattr(sys, "stdin", _BufferedStdin(frames))

    await asyncio.wait_for(stdio.run_stdio(), timeout=5)

    out = capsys.readouterr().out
    # 坏帧 → PARSE_ERROR 结构化返回
    assert "-32700" in out
    # 服务未被杀死：后续合法帧正常响应
    assert '"id": 7' in out
    assert '"ok": true' in out


# ---------------------------------------------------------------------------
# FIX: R7-A5 —— 退出时关闭同步工具线程池（防解释器退出被 join 阻塞）
# ---------------------------------------------------------------------------


def test_cleanup_resources_shuts_down_tool_executor(monkeypatch):
    """R7-A5 回归：cleanup_resources 必须关闭 _TOOL_EXECUTOR。

    ThreadPoolExecutor 非 daemon，退出从不 shutdown 时超时仍在跑的工具线程
    会被 _python_exit join，进程无法退出直至宿主强杀。
    """
    import app.mcp_server as mcp_server

    mcp_server._cleanup_done = False
    monkeypatch.setattr(mcp_server.settings, "storage_backend", "memory")

    mcp_server.cleanup_resources()
    mcp_server._cleanup_done = False  # 恢复，避免影响其他用例的幂等语义

    assert mcp_server._TOOL_EXECUTOR._shutdown is True

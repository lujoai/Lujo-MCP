"""单元与集成测试：传输启动解耦（HTTP 端口占用时 stdio 正常启动降级）。

背景：
在统一模式（--http，也是 npm 启动器默认行为）下，如果 HTTP 端口（默认 8000 或 settings.port）
被占用，旧行为会抛出 SystemExit 杀死整个进程，导致 MCP 客户端崩溃。
解耦后新行为：
- 默认端口冲突：绝不杀死进程，在 stderr/logger 输出引导警告，降级为单 transport（stdio）模式；
- 显式指定 --http-port 冲突：保持 SystemExit 报错退出；
- stdout 严格保持纯净（纯 MCP JSON-RPC 协议流，无任何日志或警告文本污染）。
"""
import asyncio
import logging
import socket
import sys
import types

import pytest

from app.config import settings
import app.mcp_server as mcp_server


def _get_occupied_port() -> tuple[socket.socket, int]:
    """创建一个占用回环端口的监听 socket，返回 (socket, port)。调用方负责 close。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def _free_port() -> int:
    """取一个当前空闲的回环端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_run_unified_transport_degrades_to_stdio_on_default_port_conflict(
    monkeypatch, caplog, capsys
):
    """当默认端口被占用时，_run_unified_transport 绝不抛出 SystemExit，而是降级启动 stdio。"""
    occupied_sock, port = _get_occupied_port()
    try:
        stdio_called = []

        async def _fake_stdio():
            stdio_called.append(True)

        monkeypatch.setattr(mcp_server, "_run_stdio_transport", _fake_stdio)

        with caplog.at_level(logging.WARNING, logger="lujo-mcp"):
            await mcp_server._run_unified_transport("127.0.0.1", port, is_default_port=True)

        # 1. 验证 stdio 服务被正常拉起
        assert len(stdio_called) == 1

        # 2. 验证警告信息内容
        warning_text = caplog.text
        assert "HTTP 端口被占用" in warning_text
        assert f"127.0.0.1:{port}" in warning_text
        assert "HTTP 采集服务未启动" in warning_text
        assert "stdio MCP 服务已正常启动" in warning_text
        assert "--http-port" in warning_text
        assert "--no-http" in warning_text

        # 3. 验证 stdout 绝对干净，没有任何非协议日志
        out, _err = capsys.readouterr()
        assert out == ""
    finally:
        occupied_sock.close()


@pytest.mark.asyncio
async def test_run_unified_transport_infers_default_port_from_settings(
    monkeypatch, caplog
):
    """当未显式传 is_default_port 时，如果 port == settings.port，自动判定为默认端口并降级。"""
    occupied_sock, port = _get_occupied_port()
    try:
        monkeypatch.setattr(mcp_server.settings, "port", port)
        stdio_called = []

        async def _fake_stdio():
            stdio_called.append(True)

        monkeypatch.setattr(mcp_server, "_run_stdio_transport", _fake_stdio)

        with caplog.at_level(logging.WARNING, logger="lujo-mcp"):
            # 不传 is_default_port，让其内部由 port == settings.port 推导
            await mcp_server._run_unified_transport("127.0.0.1", port)

        assert len(stdio_called) == 1
        assert "HTTP 端口被占用" in caplog.text
    finally:
        occupied_sock.close()


@pytest.mark.asyncio
async def test_run_unified_transport_raises_on_explicit_port_conflict(monkeypatch):
    """当用户显式指定端口（is_default_port=False）且冲突时，保持 SystemExit 报错退出。"""
    occupied_sock, port = _get_occupied_port()
    try:
        stdio_called = []

        async def _fake_stdio():
            stdio_called.append(True)

        monkeypatch.setattr(mcp_server, "_run_stdio_transport", _fake_stdio)

        with pytest.raises(SystemExit) as excinfo:
            await mcp_server._run_unified_transport("127.0.0.1", port, is_default_port=False)

        assert "HTTP 端口被占用" in str(excinfo.value)
        assert f"127.0.0.1:{port}" in str(excinfo.value)
        assert "--http-port" in str(excinfo.value)
        # stdio 服务不应被拉起
        assert len(stdio_called) == 0
    finally:
        occupied_sock.close()


@pytest.mark.asyncio
async def test_main_unified_mode_degrades_to_stdio_on_default_port_conflict(
    monkeypatch, caplog, capsys
):
    """通过 main(['--http']) 启动时，若默认端口占用，不抛 SystemExit 且正常运行 stdio。"""
    occupied_sock, port = _get_occupied_port()
    try:
        monkeypatch.setattr(mcp_server.settings, "port", port)
        monkeypatch.setattr(mcp_server.settings, "storage_backend", "memory")

        stdio_called = []

        async def _fake_stdio():
            stdio_called.append(True)

        monkeypatch.setattr(mcp_server, "_run_stdio_transport", _fake_stdio)

        with caplog.at_level(logging.WARNING, logger="lujo-mcp"):
            await mcp_server.main(["--http"])

        # 验证降级路径执行了 stdio
        assert len(stdio_called) == 1

        # 验证警告信息及指引
        assert "HTTP 端口被占用" in caplog.text
        assert "--http-port" in caplog.text
        assert "--no-http" in caplog.text

        # 验证 stdout 纯净无污染
        out, _err = capsys.readouterr()
        assert out == ""
    finally:
        occupied_sock.close()


@pytest.mark.asyncio
async def test_main_unified_mode_raises_on_explicit_port_conflict(monkeypatch):
    """通过 main(['--http', '--http-port', ...]) 显式指定端口冲突时，维持 SystemExit 退出。"""
    occupied_sock, port = _get_occupied_port()
    try:
        monkeypatch.setattr(mcp_server.settings, "storage_backend", "memory")

        stdio_called = []

        async def _fake_stdio():
            stdio_called.append(True)

        monkeypatch.setattr(mcp_server, "_run_stdio_transport", _fake_stdio)

        with pytest.raises(SystemExit) as excinfo:
            await mcp_server.main(["--http", "--http-port", str(port)])

        assert "HTTP 端口被占用" in str(excinfo.value)
        assert len(stdio_called) == 0
    finally:
        occupied_sock.close()


@pytest.mark.asyncio
async def test_degraded_stdio_registers_signal_handlers(monkeypatch):
    """统一模式降级到 stdio 时，必须主动注册 signal handlers 以保证进程可退出。"""
    occupied_sock, port = _get_occupied_port()
    try:
        registered = []
        monkeypatch.setattr(
            mcp_server, "_register_signal_handlers", lambda: registered.append(True)
        )

        async def _fake_stdio():
            pass

        monkeypatch.setattr(mcp_server, "_run_stdio_transport", _fake_stdio)

        await mcp_server._run_unified_transport("127.0.0.1", port, is_default_port=True)
        assert len(registered) == 1
    finally:
        occupied_sock.close()


@pytest.mark.asyncio
async def test_pure_stdio_mode_does_not_probe_http_port(monkeypatch):
    """当未传 --http 或传 --no-http 时，纯 stdio 模式不探测 HTTP 端口，也不报冲突。"""
    probed = []
    monkeypatch.setattr(
        mcp_server, "_http_port_conflict", lambda h, p: probed.append((h, p))
    )
    stdio_called = []

    async def _fake_stdio():
        stdio_called.append(True)

    monkeypatch.setattr(mcp_server, "_run_stdio_transport", _fake_stdio)
    monkeypatch.setattr(mcp_server.settings, "storage_backend", "memory")

    await mcp_server.main(["--no-http"])
    assert len(probed) == 0
    assert len(stdio_called) == 1


@pytest.mark.asyncio
async def test_free_port_runs_unified_without_degradation(monkeypatch):
    """空闲端口在统一模式下正常启动 HTTP 和 stdio 两个任务，不降级。"""
    port = _free_port()

    fake_main = types.ModuleType("app.main")
    fake_main.app = object()

    class FakeConfig:
        def __init__(self, app, **kwargs):
            self.app = app
            self.kwargs = kwargs

    class FakeServer:
        instances = []

        def __init__(self, config):
            self.config = config
            self.should_exit = False
            self.served = False
            self.__class__.instances.append(self)

        async def serve(self):
            self.served = True
            while not self.should_exit:
                await asyncio.sleep(0.001)

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = FakeConfig
    fake_uvicorn.Server = FakeServer
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    async def fake_stdio():
        await asyncio.sleep(0)

    monkeypatch.setattr(mcp_server, "_run_stdio_transport", fake_stdio)

    await asyncio.wait_for(
        mcp_server._run_unified_transport("127.0.0.1", port, is_default_port=True),
        timeout=2,
    )

    # 验证正常拉起 Uvicorn HTTP server
    assert FakeServer.instances[-1].served is True
    assert FakeServer.instances[-1].should_exit is True

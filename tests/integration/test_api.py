"""集成测试：HTTP API 端点（使用 FastAPI TestClient）"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="httpx 不可用，跳过 API 集成测试")

import httpx
from fastapi.testclient import TestClient

from app.main import app

# 仓库根（tests/integration/test_api.py → parents[2]）：SSE 子进程的 cwd
_REPO_ROOT = Path(__file__).resolve().parents[2]

# SSE 首行读取预算（秒）。依据（W8 第 1 步实测）：SSE 首帧 ``: connected`` 在
# subscribe 后立即 yield（不等 15s 心跳，本机 <0.5s）；10s 覆盖 CI 慢 runner
# 抖动，同时保证首行不到时以 ReadTimeout 快速失败而不是把套件挂住。
_SSE_FIRST_LINE_TIMEOUT_S = 10.0
# uvicorn 子进程 /health 就绪等待上限：本机冷启动 <3s，CI 留 15s 余量。
_UVICORN_READY_TIMEOUT_S = 15.0
# 子进程显式测试 key：环境变量优先级高于 .env env_file，不依赖也不读取
# 开发者本机 .env 的鉴权配置（无论本机 .env 是否配了 API_KEY 都稳定可测）。
_TEST_API_KEY = "test-key-w8-sse"


def _find_free_port() -> int:
    """让系统分配一个空闲端口。

    照 tests/integration/test_process_boundary.py 的既有写法在本文件内实现。
    绝不用 8000：避免撞上开发者自己的服务，也避免与 e2e conftest 的身份
    校验/端口回退逻辑互相干扰。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _terminate_gracefully(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """terminate → 宽限 → kill 阶梯收口子进程，不留孤儿。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _wait_http_ready(port: int, proc: subprocess.Popen, timeout: float = _UVICORN_READY_TIMEOUT_S) -> None:
    """有界轮询 /health 直到 200；超时或子进程提前退出时抛 AssertionError。"""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            raise AssertionError(f"uvicorn 子进程提前退出（exit_code={rc}），/health 未就绪")
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
            if r.status_code == 200:
                return
            last_err = f"status={r.status_code}"
        except httpx.HTTPError as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.2)
    raise AssertionError(f"uvicorn /health 在 {timeout}s 内未就绪，最后错误: {last_err}")


@pytest.fixture
def client(monkeypatch):
    # 平台隔离：Windows 上 os.environ["API_KEY"]="" 等价 unset，settings 回落读取
    # .env 真实 API_KEY，使 AuthMiddleware 以 401 拒绝本文件的 HTTP 请求。此处把
    # settings 单例改为「未配置」，让鉴权实时判定关闭（每次请求实时读 settings，
    # monkeypatch 即时生效，测试后自动恢复；本文件无鉴权前置条件的用例不受影响）。
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "api_keys", "")
    return TestClient(app)


class TestHealthEndpoint:

    def test_health_ok(self, client):
        # /health 仅返回状态，不暴露内部配置（S7 / A1）
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"status"}
        assert data["status"] in ("ok", "degraded", "unhealthy")

    def test_internal_health_exposes_service(self, client, monkeypatch):
        # /internal/health 返回详细配置，供集群内访问
        # TestClient 的 client.host 为非 IP 字符串 "testclient"，_is_internal_ip 恒 False，
        # 这里 monkeypatch 模拟内网来源（该端点的 IP 门禁逻辑由 test_internal_health_forbidden 覆盖）
        monkeypatch.setattr("app.main._is_internal_ip", lambda request: True)
        resp = client.get("/internal/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "lujo-mcp"
        assert "version" in data
        assert data["status"] in ("ok", "degraded", "unhealthy")

    def test_internal_health_forbidden_external(self, client, monkeypatch):
        # 非内网来源（且未启用鉴权）应 fail-closed 返回 403
        monkeypatch.setattr("app.main._is_internal_ip", lambda request: False)
        resp = client.get("/internal/health")
        assert resp.status_code == 403


class TestDebugEndpoint:

    def test_debug_run_returns_schema(self, client):
        resp = client.post("/api/debug/run", json={"payload": {"foo": "bar", "n": 1}})
        assert resp.status_code == 200
        data = resp.json()
        # 验证字段完整
        assert "request_id" in data
        assert "result" in data
        assert "trace" in data
        assert "context" in data
        # 验证 context 结构
        ctx = data["context"]
        assert ctx["request_id"] == data["request_id"]
        assert "flow" in ctx
        assert "errors" in ctx

    def test_legacy_debug_endpoint_returns_410(self, client):
        """便捷入口 POST /debug 已废弃收敛到 /api/debug/run：410 + 替代提示。"""
        resp = client.post("/debug", json={"foo": "bar"})
        assert resp.status_code == 410
        data = resp.json()
        assert data["error"] == "endpoint_removed"
        assert data["replacement"] == "/api/debug/run"


class TestMetricsEndpoint:

    def test_metrics_format(self, client):
        # 先打几个请求，产生指标
        client.get("/health")
        client.post("/api/debug/run", json={"payload": {"x": 1}})

        resp = client.get("/metrics")
        assert resp.status_code == 200
        text = resp.text
        # Prometheus 格式检查
        assert "# TYPE http_requests_total counter" in text
        assert "http_requests_total{" in text


class TestMCPProtocol:
    """验证 MCP Streamable HTTP 传输（含会话握手）"""

    def _handshake(self, client):
        """完成 initialize 握手，返回 session id"""
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp.status_code == 200
        assert "Mcp-Session-Id" in resp.headers
        # 发送 notifications/initialized
        sid = resp.headers["Mcp-Session-Id"]
        client.post(
            "/mcp",
            headers={"Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return sid

    def test_mcp_initialize_returns_session(self, client):
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp.status_code == 200
        data = resp.json()
        assert "protocolVersion" in data["result"]
        assert "capabilities" in data["result"]
        assert "Mcp-Session-Id" in resp.headers

    def test_mcp_ping_without_session_rejected(self, client):
        """未握手直接调用应被拒（符合规范）"""
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 400

    def test_mcp_tools_list(self, client):
        sid = self._handshake(client)
        resp = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 200
        data = resp.json()
        tools = data["result"]["tools"]
        names = [t["name"] for t in tools]
        assert "debug" in names
        assert "context" in names

    def test_mcp_tool_call_debug(self, client):
        sid = self._handshake(client)
        resp = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": sid},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "debug", "arguments": {"payload": {"test": 1}}},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["isError"] is False
        assert "content" in data["result"]

    def test_mcp_unknown_method(self, client):
        sid = self._handshake(client)
        resp = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "id": 5, "method": "nonexistent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data

    def test_mcp_sse_stream(self):
        """GET /mcp 带 SSE Accept 应建立事件流，首行为 ': connected' 注释帧。

        为什么不用 TestClient（W8 第 1 步实测）：starlette 1.3.1 的
        ``_TestClientTransport.handle_request`` 以
        ``portal.call(self.app, scope, receive, send)`` 等待**整个** ASGI 调用
        结束才构造响应——对永不结束的 SSE 流，响应头永远拿不到（对照实测：
        非 SSE 的 GET /mcp 4ms 返回、SSE Accept 无 session 的 400 JSON 3ms
        返回，完整 SSE 分支 >10s 仍阻塞在 stream ``__enter__``）。原 skip 的
        reason（"TestClient 中会阻塞"）属 harness 局限而非环境缺失，故改用
        真实 uvicorn 子进程（随机空闲端口）+ httpx 流式客户端；首行读取带
        10s 硬上限，不到即 ReadTimeout 用例失败，套件不会挂死。
        """
        port = _find_free_port()
        env = {
            # 继承 tests/integration/conftest.py 已强制的 memory 后端与 KB 关闭
            # （conftest 在导入期写入 os.environ，子进程直接继承，不另设一套）
            **os.environ,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "API_KEY": _TEST_API_KEY,
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(_REPO_ROOT),
        )
        try:
            _wait_http_ready(port, proc)

            base = f"http://127.0.0.1:{port}"
            auth = {"Authorization": f"Bearer {_TEST_API_KEY}"}

            # 握手（与 TestClient 侧 _handshake 相同的两步协议）
            resp = httpx.post(
                f"{base}/mcp",
                headers=auth,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                timeout=5.0,
            )
            assert resp.status_code == 200, resp.text
            sid = resp.headers["Mcp-Session-Id"]
            resp = httpx.post(
                f"{base}/mcp",
                headers={**auth, "Mcp-Session-Id": sid},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout=5.0,
            )
            assert resp.status_code in (200, 202), resp.text

            with httpx.stream(
                "GET",
                f"{base}/mcp",
                headers={**auth, "Mcp-Session-Id": sid, "Accept": "text/event-stream"},
                timeout=httpx.Timeout(_SSE_FIRST_LINE_TIMEOUT_S, connect=5.0),
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                # 加强：media type 段精确（排除 "text/event-streamX" 这类意外值）
                assert resp.headers["content-type"].split(";", 1)[0].strip() == "text/event-stream"
                # 读取首行（: connected 注释）；10s 内不到 → ReadTimeout → 用例失败
                first = next(resp.iter_lines())
                assert first.strip() == ": connected"
                # 加强：首行必须是 SSE 注释帧（":" 开头）而非数据帧（data:/event:）
                assert first.lstrip().startswith(":")
        finally:
            _terminate_gracefully(proc)
            # 收口核对：不得留下孤儿子进程（异常路径也执行）
            assert proc.poll() is not None, "SSE 测试的 uvicorn 子进程收口失败（残留孤儿）"

    def test_mcp_delete_session(self, client):
        sid = self._handshake(client)
        resp = client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": sid})
        assert resp.status_code == 204
        # 删除后再次使用应被拒
        resp2 = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
        )
        assert resp2.status_code == 404

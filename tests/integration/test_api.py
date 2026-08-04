"""集成测试：HTTP API 端点（使用 FastAPI TestClient）"""
import pytest

pytest.importorskip("httpx", reason="httpx 不可用，跳过 API 集成测试")

from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealthEndpoint:

    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "ai-debug-mcp"
        assert data["status"] in ("ok", "degraded", "unhealthy")


class TestDebugEndpoint:

    def test_debug_returns_schema(self, client):
        resp = client.post("/debug", json={"foo": "bar", "n": 1})
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


class TestMetricsEndpoint:

    def test_metrics_format(self, client):
        # 先打几个请求，产生指标
        client.get("/health")
        client.post("/debug", json={"x": 1})

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

    @pytest.mark.skip(reason="SSE 长连接流在 TestClient 中会阻塞，需要手动验证")
    def test_mcp_sse_stream(self, client):
        """GET /mcp 带 SSE Accept 应建立事件流"""
        sid = self._handshake(client)
        with client.stream("GET", "/mcp", headers={
            "Mcp-Session-Id": sid,
            "Accept": "text/event-stream",
        }) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            # 读取首行（: connected 注释）
            first = next(resp.iter_lines())
            assert first.strip() == ": connected"

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

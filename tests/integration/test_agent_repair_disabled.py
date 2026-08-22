"""集成测试：agent_enabled=False 时端点返回 501。

验证 feature flag 关闭时的零行为变更：
- POST /api/debug/repair/async 返回 501
- GET /api/debug/repair/result/{job_id} 返回 501
- MCP 工具 repair_async_handler 返回 {"error": "agent disabled"}
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def disable_agent(monkeypatch):
    """强制关闭 agent_enabled。"""
    monkeypatch.setattr(settings, "agent_enabled", False)
    # 平台隔离：Windows 上 os.environ["API_KEY"]="" 等价 unset，settings 回落读取
    # .env 真实 API_KEY，使 AuthMiddleware 401 拒绝本文件的 HTTP 请求。此处把
    # settings 单例改为「未配置」，让鉴权实时判定关闭（REST 测试即时生效；MCP 工具
    # 测试不经 HTTP 中间件，不受影响）。
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "api_keys", "")


class TestRepairEndpointsDisabled:
    """REST 端点在 agent_enabled=False 时返回 501。"""

    def test_repair_async_returns_501(self):
        client = TestClient(app)
        response = client.post("/api/debug/repair/async", json={"request_id": "r1"})
        assert response.status_code == 501

    def test_repair_result_returns_501(self):
        client = TestClient(app)
        response = client.get("/api/debug/repair/result/fake-job-id")
        assert response.status_code == 501


class TestRepairMcpToolDisabled:
    """MCP 工具在 agent_enabled=False 时返回 error。"""

    @pytest.mark.asyncio
    async def test_repair_async_handler_returns_error(self):
        from app.mcp.tools.repair_api import repair_async_handler

        result = await repair_async_handler({"request_id": "r1"})
        assert "error" in result
        assert "disabled" in result["error"]

    @pytest.mark.asyncio
    async def test_repair_result_handler_returns_error(self):
        from app.mcp.tools.repair_api import repair_result_handler

        result = await repair_result_handler({"job_id": "fake-id"})
        assert "error" in result
        assert "disabled" in result["error"]

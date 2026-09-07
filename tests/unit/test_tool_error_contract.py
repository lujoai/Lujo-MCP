"""R7 回归测试 —— 工具失败契约：stdio 返回 isError=true，HTTP 同语义。

覆盖报告验收项：
- 业务级失败（handler 返回含 error 键的 dict）：stdio 的 CallToolResult
  isError=True、HTTP 的 make_response isError=True，且载荷结构保持不变；
- 正常无数据（diagnose_issue 的 found=false）：两条传输层都是成功结果
  （不把 found=false 误判为错误）；
- 未知工具、handler 抛异常、被回调捕获的超时：stdio isError=True。
"""
from __future__ import annotations

import json
import time

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from app.config import settings
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry, register_tool
from app.mcp.protocol.tool_errors import ToolExecutionError, is_tool_failure_result
from app.mcp.tools import register_all_tools


@pytest.fixture(autouse=True)
def _registered_tools():
    register_all_tools()
    # 与 test_diagnose_issue 相同：重置进程级 memory trace 存储保证用例独立
    from app.runtime.core.storage import factory as _storage_factory

    _storage_factory._trace_store = None
    yield
    _storage_factory._trace_store = None


# ── 失败判定契约 ──────────────────────────────────────────────────────


class TestFailureResultContract:
    def test_error_key_means_failure(self):
        assert is_tool_failure_result({"error": "boom"}) is True
        assert is_tool_failure_result({"error": "boom", "count": 0}) is True

    def test_found_false_is_success(self):
        assert is_tool_failure_result({"found": False, "message": "not found"}) is False

    def test_normal_payload_is_success(self):
        assert is_tool_failure_result({"found": True, "trace_id": "err-x"}) is False
        assert is_tool_failure_result({"count": 0, "results": []}) is False
        assert is_tool_failure_result(None) is False
        assert is_tool_failure_result("text") is False


# ── HTTP 协议层（app/mcp/protocol/server.py）──────────────────────────


async def _http_call(name: str, arguments: dict) -> dict:
    req = JSONRPCRequest(
        id="r7-1",
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )
    return await _handle_tools_call(req)


class TestHttpErrorSemantics:
    @pytest.mark.asyncio
    async def test_handler_error_dict_is_marked_is_error(self):
        """resolve_stack 空帧：业务失败 → isError=true（修复前恒 false）。"""
        resp = await _http_call("resolve_stack", {"frames": []})
        assert resp.get("error") is None
        assert resp["result"]["isError"] is True
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["error"] == "frames 必须是非空数组"

    @pytest.mark.asyncio
    async def test_found_false_is_not_error(self):
        """diagnose 无数据：found=false 是正常结果，isError=false。"""
        resp = await _http_call("diagnose_issue", {})
        assert resp.get("error") is None
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["found"] is False


# ── stdio（官方 MCP SDK 适配层，app/mcp_server.py）────────────────────


def _call_tool_request(name: str, arguments: dict) -> CallToolRequest:
    return CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )


class TestStdioIsErrorSemantics:
    """官方 SDK 的 ServerResult.model_dump 是扁平结构（无 result 包装层）。"""

    @staticmethod
    def _dump(result) -> dict:
        return result.model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_handler_error_dict_yields_is_error_true(self):
        """业务回调捕获的失败：外层 CallToolResult.isError=True。"""
        import app.mcp_server as stdio

        result = await stdio.server.request_handlers[CallToolRequest](
            _call_tool_request("resolve_stack", {"frames": []})
        )
        dumped = self._dump(result)
        assert dumped["isError"] is True
        text = dumped["content"][0]["text"]
        assert json.loads(text)["error"] == "frames 必须是非空数组"

    @pytest.mark.asyncio
    async def test_unknown_tool_yields_is_error_true(self):
        import app.mcp_server as stdio

        result = await stdio.server.request_handlers[CallToolRequest](
            _call_tool_request("no_such_tool", {})
        )
        dumped = self._dump(result)
        assert dumped["isError"] is True
        assert "未知工具" in dumped["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_found_false_is_success_on_stdio(self):
        """无数据（found=false）在 stdio 上也是成功结果。"""
        import app.mcp_server as stdio

        result = await stdio.server.request_handlers[CallToolRequest](
            _call_tool_request("diagnose_issue", {})
        )
        dumped = self._dump(result)
        assert dumped["isError"] is False
        payload = json.loads(dumped["content"][0]["text"])
        assert payload["found"] is False

    @pytest.mark.asyncio
    async def test_captured_timeout_yields_is_error_true(self, monkeypatch):
        """被回调捕获的超时：isError=True 且带 _timed_out 标记。"""
        import app.mcp_server as stdio

        def _slow(_arguments):
            time.sleep(2)
            return {"ok": True}

        register_tool(
            "r7_slow_tool",
            description="test-only slow tool",
            handler=_slow,
            inputSchema={"type": "object"},
        )
        try:
            monkeypatch.setattr(settings, "tool_timeout_seconds", 0.2)
            result = await stdio.server.request_handlers[CallToolRequest](
                _call_tool_request("r7_slow_tool", {})
            )
        finally:
            _tool_registry.pop("r7_slow_tool", None)

        dumped = self._dump(result)
        assert dumped["isError"] is True
        payload = json.loads(dumped["content"][0]["text"])
        assert payload["_timed_out"] is True

    @pytest.mark.asyncio
    async def test_success_payload_shape_unchanged(self):
        """成功路径不回归：isError=False + 原有 JSON 载荷结构。"""
        import app.mcp_server as stdio

        result = await stdio.server.request_handlers[CallToolRequest](
            _call_tool_request("list_recent_traces", {"limit": 1})
        )
        dumped = self._dump(result)
        assert dumped["isError"] is False
        payload = json.loads(dumped["content"][0]["text"])
        assert "count" in payload and "traces" in payload

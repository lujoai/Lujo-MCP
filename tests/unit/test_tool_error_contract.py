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
from app.mcp.protocol.tool_errors import (
    ToolExecutionError,
    conclusion_tool_is_failure,
    is_tool_failure_result,
)
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


# ── R8：结论型工具（verify / verify_ui）的失败判定 ────────────────────


@pytest.fixture(autouse=True)
def _fresh_pools_for_heavy(monkeypatch):
    """隔离池状态：W4-4 后 cleanup_resources 会 begin_close 共享池（生产正确
    语义：进程退出后不再接纳）。本文件的重活调用需要未 closing 的池，故
    每用例注入全新池（light/heavy），避免同进程内测试顺序污染。"""
    from app.mcp.protocol.executor_lifecycle import SlotPool
    import app.mcp.protocol.server as protocol_server
    import app.mcp_server as stdio

    light = SlotPool("light", 8)
    heavy = SlotPool("heavy", 2)
    monkeypatch.setattr(protocol_server, "_light_pool", light)
    monkeypatch.setattr(protocol_server, "_heavy_pool", heavy)
    monkeypatch.setattr(stdio, "_light_pool", light)
    monkeypatch.setattr(stdio, "_heavy_pool", heavy)
    yield


class TestConclusionToolContract:
    """验证结论不是工具失败。

    全局契约「dict 含非空 error 键 ⇒ 失败」对 verify / verify_ui 不成立：
    它们的载荷本身就是答案（matched / diffs / silent_failure），error 只是原因
    说明。标成 isError=true 会让宿主重试而不是读结论——包括「验证不通过」这种
    最有价值的结论。两条传输都必须给出同一判定。
    """

    def test_conclusion_payload_is_not_failure(self):
        assert conclusion_tool_is_failure(
            {"matched": False, "diffs": [], "silent_failure": False,
             "error": "must provide spec or spec_id"}
        ) is False

    def test_bare_error_dict_is_still_failure(self):
        """谓词不是"该工具永不失败"的口子：非结论形状仍按全局契约判失败。"""
        assert conclusion_tool_is_failure({"error": "boom"}) is True

    def test_non_dict_is_not_failure(self):
        assert conclusion_tool_is_failure(None) is False
        assert conclusion_tool_is_failure("text") is False

    @pytest.mark.parametrize(
        ("tool_name", "arguments", "expected_error_text"),
        [
            ("verify_ui", {}, "must provide spec or spec_id"),
            ("verify_ui", {"spec": {"kind": "http"}}, "spec.kind must be 'ui'"),
            # verify 的 schema 要求 actual，必须带上才能走到结论分支
            ("verify", {"actual": {"status_code": 200}, "spec_id": "no-such-spec"},
             "not found"),
        ],
    )
    @pytest.mark.asyncio
    async def test_http_marks_conclusion_as_success(
        self, tool_name, arguments, expected_error_text
    ):
        resp = await _http_call(tool_name, arguments)
        assert resp.get("error") is None
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert expected_error_text in payload["error"]
        assert payload["matched"] is False

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("verify_ui", {}),
            ("verify_ui", {"spec": {"kind": "http"}}),
            ("verify", {"actual": {"status_code": 200}, "spec_id": "no-such-spec"}),
        ],
    )
    @pytest.mark.asyncio
    async def test_stdio_marks_conclusion_as_success(self, tool_name, arguments):
        """stdio 与 HTTP 同口径：结论不得被包成 ToolExecutionError。"""
        import app.mcp_server as stdio

        result = await stdio.server.request_handlers[CallToolRequest](
            _call_tool_request(tool_name, arguments)
        )
        dumped = result.model_dump(mode="json")
        assert dumped["isError"] is False
        payload = json.loads(dumped["content"][0]["text"])
        assert payload["matched"] is False
        assert payload["error"]

    def test_registry_wires_the_predicate_for_both_tools(self):
        """守卫：谓词必须真的挂到注册表上，否则传输层回落到全局契约。"""
        from app.mcp.tools import register_all_tools

        register_all_tools()
        for name in ("verify", "verify_ui"):
            assert _tool_registry[name]["is_failure"] is conclusion_tool_is_failure

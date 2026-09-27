"""doctor MCP 工具与能力可见性单测（P0-B「浏览器能力可见性」方案 A）。

覆盖：
1. doctor 返回结构：checks[]（name/ok/detail）+ summary（ok_count/fail_count）
2. 七项自检齐全，工具本身永不抛异常
3. 无 playwright 环境下 playwright 库/chromium 二进制两项为 ok:false
   （monkeypatch sys.modules 模拟，与本机是否真实安装 playwright 无关）
4. doctor 注册面：常驻可见（无 availability 过滤）、category=diagnostic、角色映射齐全
5. verify_ui/auto_test 无能力分支返回统一 CAPABILITY_MISSING 载荷（dict，不 raise）
"""
import asyncio
import json
import sys

import pytest

from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import (
    _handle_tools_call,
    _tool_registry,
    get_agent_visible_tools,
)
from app.mcp.protocol.tool_errors import (
    conclusion_tool_is_failure,
    is_tool_failure_result,
)
from app.mcp.tools import TOOL_ROLE_REQUIREMENTS, register_all_tools
from app.mcp.tools.auto_test_api import auto_test_handler
from app.mcp.tools.doctor_api import DOCTOR_DEF, doctor_handler

EXPECTED_CHECK_NAMES = {
    "playwright_library",
    "chromium_binary",
    "heavy_worker_entry",
    "http_listen",
    "ui_url_policy",
    "kb_persistence",
    "vector_embedding",
}


@pytest.fixture
def no_playwright(monkeypatch):
    """把 playwright 相关模块顶成 None，使 import 必然 ImportError。

    sys.modules 中值为 None 的条目会让 ``from X import Y`` 抛
    ``ImportError: import of X halted; None in sys.modules``——无论本机是否
    真实安装 playwright，①② 自检与 auto_test 无能力分支都确定性走向降级，
    且单测不触碰真实 playwright driver。
    """
    for mod in ("playwright", "playwright.sync_api", "playwright.async_api"):
        monkeypatch.setitem(sys.modules, mod, None)


# ── 1/2/3. doctor_handler 结构与七项自检 ──


class TestDoctorHandler:
    def test_returns_checks_and_summary(self, no_playwright):
        result = doctor_handler({})
        assert set(result.keys()) == {"checks", "summary"}
        names = [c["name"] for c in result["checks"]]
        assert len(names) == 7
        assert set(names) == EXPECTED_CHECK_NAMES

    def test_each_check_has_name_ok_detail(self, no_playwright):
        result = doctor_handler({})
        for check in result["checks"]:
            assert set(check.keys()) == {"name", "ok", "detail"}
            assert isinstance(check["ok"], bool)
            assert isinstance(check["detail"], str) and check["detail"]

    def test_summary_counts_consistent(self, no_playwright):
        result = doctor_handler({})
        ok_count = sum(1 for c in result["checks"] if c["ok"])
        assert result["summary"]["ok_count"] == ok_count
        assert result["summary"]["fail_count"] == len(result["checks"]) - ok_count
        assert result["summary"]["ok_count"] + result["summary"]["fail_count"] == 7

    def test_no_playwright_checks_fail_but_tool_does_not_raise(self, no_playwright):
        """无 playwright 环境：库/二进制两项 ok:false，工具整体不抛异常。"""
        result = doctor_handler({})
        by_name = {c["name"]: c for c in result["checks"]}
        assert by_name["playwright_library"]["ok"] is False
        assert "playwright" in by_name["playwright_library"]["detail"]
        assert by_name["chromium_binary"]["ok"] is False

    def test_doctor_payload_is_not_tool_failure(self, no_playwright):
        """doctor 载荷无 error 键：按全局契约是成功结果（isError=false）。"""
        assert not is_tool_failure_result(doctor_handler({}))


# ── 4. doctor 注册面 ──


class TestDoctorRegistration:
    def test_registered_resident_visible(self):
        """常驻可见：无 availability 过滤、agent_visible=True。"""
        register_all_tools()
        tool = _tool_registry["doctor"]
        assert tool["availability"] is None
        assert tool["agent_visible"] is True
        assert tool["category"] == "diagnostic"

    def test_in_tools_list(self):
        register_all_tools()
        assert "doctor" in {t["name"] for t in get_agent_visible_tools()}

    def test_role_requirement_declared(self):
        """角色映射必须显式声明（否则 mcp_routes fail-closed 收紧为 admin）。"""
        register_all_tools()
        assert TOOL_ROLE_REQUIREMENTS["doctor"] == ("admin", "developer", "viewer")

    def test_def_schema_accepts_empty_arguments(self):
        assert DOCTOR_DEF["name"] == "doctor"
        assert DOCTOR_DEF["inputSchema"]["type"] == "object"
        assert DOCTOR_DEF["inputSchema"].get("required", []) == []


# ── 5. CAPABILITY_MISSING 统一能力缺失载荷 ──


class TestCapabilityMissingPayload:
    def test_auto_test_no_playwright_returns_structured_failure(self, no_playwright):
        """auto_test 无能力分支：统一载荷、无结论键、dict 返回（不 raise）。"""
        result = asyncio.run(auto_test_handler({"url": "http://127.0.0.1:9/x"}))
        assert result["error_code"] == "CAPABILITY_MISSING"
        assert result["retryable"] is False
        assert "playwright 未安装" in result["error"]
        assert "matched" not in result
        assert "diffs" not in result
        assert result["install"]["source"] == (
            "pip install playwright && playwright install chromium"
        )
        assert "冻结二进制" in result["install"]["npm_frozen"]

    def test_capability_missing_is_execution_failure_by_both_contracts(self, no_playwright):
        """能力缺失=执行失败：全局契约与结论型谓词都判失败（isError=true）。"""
        result = asyncio.run(auto_test_handler({"url": "http://127.0.0.1:9/x"}))
        assert is_tool_failure_result(result) is True
        # verify_ui 注册的 conclusion_tool_is_failure 对非结论形状回落同一结论
        assert conclusion_tool_is_failure(result) is True


# ── MCP 全链路 ──


class TestDoctorViaMCP:
    def test_tools_call_roundtrip(self, no_playwright):
        register_all_tools()
        req = JSONRPCRequest(
            id="req-doctor",
            method="tools/call",
            params={"name": "doctor", "arguments": {}},
        )
        resp = asyncio.run(_handle_tools_call(req))
        assert resp.get("error") is None
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["summary"]["ok_count"] + payload["summary"]["fail_count"] == 7
        assert {c["name"] for c in payload["checks"]} == EXPECTED_CHECK_NAMES

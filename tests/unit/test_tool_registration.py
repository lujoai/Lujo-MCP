"""工具注册单测：验证 HTTP / stdio 工具注册与导出保持一致"""
import asyncio

import pytest

from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry, register_tool
from app.mcp.tools import register_all_tools


def test_all_tools_registered():
    register_all_tools()
    names = set(_tool_registry.keys())
    expected = {
        # 既有 4 个
        "debug", "context", "trace", "stacktrace",
        # M3-M9 新增 7 个
        "ingest_network", "get_network_trace",
        "get_blame_for_frame", "get_recent_diff",
        "ingest_silent_failure", "ingest_error",
        "ingest_console",
        "get_related_specs",
        # V3 新增
        "verify",
        # FR14 新增
        "verify_ui",
        "auto_test",
        # v0.7.3 统一诊断入口 + 近期错误查询工具
        "diagnose_issue",
        "list_recent_traces",
        "search_logs",
        # v0.7.5 OpenAPI 一键生成断言规范
        "ingest_specs",
    }
    missing = expected - names
    assert not missing, f"未注册的工具: {missing}"
    assert len(names) >= 18


def test_each_registered_tool_has_handler():
    register_all_tools()
    for name, tool in _tool_registry.items():
        assert callable(tool["handler"]), f"{name} handler 不可调用"
        assert tool["inputSchema"] is not None, f"{name} 缺 inputSchema"


def test_stdio_exports_dynamic_registered_tools():
    """stdio tools/list 与 HTTP 一致：只暴露 Agent-facing 工具。

    v0.7.3: SDK 上报类工具（ingest_*，agent_visible=False）不再出现在
    tools/list 中（减少宿主 AI 选择噪音），但 registry 保留、tools/call
    按名调用仍可执行。
    """
    import app.mcp_server as mcp_server

    tools = asyncio.run(mcp_server.list_tools())
    names = {tool.name for tool in tools}
    # Agent-facing 核心工具必须在列
    assert "verify" in names
    assert "verify_ui" in names
    assert "diagnose_issue" in names
    assert "list_recent_traces" in names
    assert "search_logs" in names
    # SDK 上报类工具不进清单
    assert "ingest_console" not in names
    assert "ingest_error" not in names


def test_sdk_ingest_tools_filtered_from_tools_list_but_callable():
    """tools/list 过滤 SDK 工具，但 tools/call 按名调用照常执行（REST/SDK 链路不受影响）。"""
    import json as _json

    from app.mcp.protocol.server import get_agent_visible_tools

    register_all_tools()
    visible_names = {t["name"] for t in get_agent_visible_tools()}
    registry_names = set(_tool_registry.keys())
    sdk_tools = {"ingest_network", "ingest_error", "ingest_console", "ingest_silent_failure"}
    # 过滤生效
    assert sdk_tools & visible_names == set()
    # registry 保留（tools/call 可调用）
    assert sdk_tools <= registry_names
    # 公开清单仍包含核心 Agent 工具
    for core in ("diagnose_issue", "list_recent_traces", "search_logs",
                 "context", "trace", "stacktrace", "verify", "verify_ui"):
        assert core in visible_names, f"核心工具 {core} 不应在 tools/list 中缺席"

    # tools/call 直接调用被过滤的 SDK 工具仍可执行（ingest_error 落 memory 存储）
    req = JSONRPCRequest(
        id="req-vis",
        method="tools/call",
        params={"name": "ingest_error", "arguments": {"exc_type": "E", "message": "m"}},
    )
    resp = asyncio.run(_handle_tools_call(req))
    assert resp.get("error") is None
    payload = _json.loads(resp["result"]["content"][0]["text"])
    assert payload["saved"] is True


# ---------------------------------------------------------------------------
# FIX: P1-C5 —— inputSchema 轻量校验（参数错误 → -32602，LLM 自纠错依据）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_schema_test_registry():
    saved = dict(_tool_registry)
    yield
    _tool_registry.clear()
    _tool_registry.update(saved)


def _register_schema_test_tool():
    """注册带 required + 类型声明的测试工具（handler 索引访问 file/line）。"""
    def handler(arguments):
        return {"file": arguments["file"], "line": arguments["line"]}

    register_tool(
        "schema_test_tool",
        "schema validation test tool",
        handler,
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "tags": {"type": "array"},
                "note": {"type": "string"},
            },
            "required": ["file", "line"],
        },
    )


async def _call(arguments):
    _register_schema_test_tool()
    req = JSONRPCRequest(
        id="req-c5",
        method="tools/call",
        params={"name": "schema_test_tool", "arguments": arguments},
    )
    return await _handle_tools_call(req)


def _unwrap_handler_result(resp: dict) -> dict:
    """解包 MCP content 包装：handler 返回值被序列化为 content[0].text。"""
    import json as _json

    return _json.loads(resp["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_missing_required_param_returns_invalid_params():
    """缺 required 参数 → -32602（此前 KeyError 被吞成 TOOL_INTERNAL）。"""
    resp = await _call({"file": "a.py"})  # 缺 line
    assert resp["error"]["code"] == -32602
    assert "line" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_wrong_type_returns_invalid_params():
    """参数类型错误 → -32602（此前 TypeError/ValueError 被吞成 TOOL_INTERNAL）。"""
    resp = await _call({"file": "a.py", "line": "not-a-number"})
    assert resp["error"]["code"] == -32602
    assert "line" in resp["error"]["message"]

    resp2 = await _call({"file": 123, "line": 1})
    assert resp2["error"]["code"] == -32602
    assert "file" in resp2["error"]["message"]


@pytest.mark.asyncio
async def test_arguments_non_dict_returns_invalid_params():
    """arguments 为 list/str/null → -32602（此前 AttributeError 被吞成 TOOL_INTERNAL）。"""
    for bad in ([], "x", None):
        _register_schema_test_tool()
        req = JSONRPCRequest(
            id="req-c5b",
            method="tools/call",
            params={"name": "schema_test_tool", "arguments": bad},
        )
        resp = await _handle_tools_call(req)
        assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_explicit_null_typed_param_returns_invalid_params():
    """显式 null 的类型化参数 → -32602（此前 realpath(None) TypeError → TOOL_INTERNAL）。"""
    resp = await _call({"file": None, "line": 1})
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_valid_arguments_pass_validation():
    """合法参数照常到达 handler（含未声明的额外参数——保持旧兼容）。"""
    resp = await _call({"file": "a.py", "line": 12, "unknown_extra": "ok"})
    handler_result = _unwrap_handler_result(resp)
    assert handler_result["file"] == "a.py"
    assert handler_result["line"] == 12


@pytest.mark.asyncio
async def test_integer_accepts_integral_float():
    """integer 容忍整值 float（JSON 20.0 反序列化为 float，handler 本可处理）。"""
    resp = await _call({"file": "a.py", "line": 20.0})
    handler_result = _unwrap_handler_result(resp)
    assert handler_result["line"] == 20.0


@pytest.mark.asyncio
async def test_boolean_not_accepted_as_integer():
    """bool 是 int 子类，integer/number 不接受布尔值。"""
    resp = await _call({"file": "a.py", "line": True})
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_repair_async_trace_id_only_still_accepted():
    """repair_async 的 request_id/trace_id 二选一：只传 trace_id 不被 required 拒绝。

    schema 的 required 已修为 []（二选一无法用 required 表达），由 handler
    的 "must provide request_id or trace_id" 运行时检查兜底。
    """
    register_all_tools()
    req = JSONRPCRequest(
        id="req-c5c",
        method="tools/call",
        params={"name": "repair_async", "arguments": {"trace_id": "trace-x"}},
    )
    resp = await _handle_tools_call(req)
    # 到达 handler 即未被校验层拒绝（协议层 error 为 None）；handler 返回的
    # error dict 取决于 agent_enabled 配置（False → "agent disabled"，
    # True → "request ... not found"），两者都证明参数通过了校验
    assert resp.get("error") is None
    handler_result = _unwrap_handler_result(resp)
    assert "error" in handler_result


@pytest.mark.asyncio
async def test_registered_tools_schema_required_subset_of_properties():
    """契约守护：所有已注册工具的 required 字段必须在 properties 中声明。"""
    register_all_tools()
    for name, tool in _tool_registry.items():
        schema = tool["inputSchema"] or {}
        props = set((schema.get("properties") or {}).keys())
        for field_name in schema.get("required") or []:
            assert field_name in props, (
                f"工具 {name} 的 required 字段 {field_name} 未在 properties 声明"
            )

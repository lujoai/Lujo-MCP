"""统一诊断入口 diagnose_issue + 最近错误查询工具测试（v0.7.3）。

覆盖任务验收项：
1. diagnose_issue 无参数读取最新错误（真实 memory 存储链路：save_trace 写入 → 读取）
2. diagnose_issue 指定 request_id
3. diagnose_issue query 关键词匹配
4. diagnose_issue 没有数据时返回 found=false + setup_hint + next_step
5. diagnose_issue 错误参数校验（-32602）
6. list_recent_traces MCP 调用
7. search_logs MCP 调用
"""
import json
import time

import pytest

from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry
from app.mcp.tools import register_all_tools
from app.runtime.core.trace_repo import save_trace


@pytest.fixture(autouse=True)
def _registered_tools():
    register_all_tools()
    # conftest 只清 errors._recent；memory trace 存储是进程级单例，
    # 诊断兜底会经 list_request_ids 读到前面测试残留的 trace，
    # 这里每个用例前重置为全新 memory 后端，保证用例独立。
    from app.runtime.core.storage import factory as _storage_factory

    _storage_factory._trace_store = None
    yield
    _storage_factory._trace_store = None


def _seed_error(
    exc_type: str = "TypeError",
    message: str = "Cannot read properties of undefined (reading 'token')",
    source: str = "browser-sdk",
    trace_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """真实链路写入一条错误（errors 缓冲 + trace 存储），返回 error_id。"""
    return save_trace(
        exc_type=exc_type,
        message=message,
        frames=[{"file": "src/login.js", "line": 42, "function": "handleSubmit"}],
        source=source,
        trace_kind="exception",
        trace_id=trace_id,
        session_id=session_id,
    )


async def _call_tool(name: str, arguments: dict) -> dict:
    """经协议层 tools/call 调用并解包 handler 结果（同时验证 schema 校验通过）。"""
    req = JSONRPCRequest(
        id="diag-1",
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )
    resp = await _handle_tools_call(req)
    assert resp.get("error") is None, f"协议层报错: {resp.get('error')}"
    return json.loads(resp["result"]["content"][0]["text"])


# ── diagnose_issue ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diagnose_no_args_returns_latest_error():
    """无参数 → 自动定位最近一次真实错误并返回完整调试上下文。"""
    _seed_error(message="old error")
    time.sleep(0.01)
    latest_id = _seed_error(message="Cannot read properties of undefined (reading 'token')")

    result = await _call_tool("diagnose_issue", {})

    assert result["found"] is True
    assert result["trace_id"] == latest_id
    assert result["source"] == "latest"
    assert result["summary"]["type"] == "TypeError"
    assert "token" in result["summary"]["message"]
    # 真实存储链路：debug_context 应由 save_trace 写入的数据构建
    assert result["debug_context"], "debug_context 不应为空"
    assert result["debug_context"].get("trace_id") == latest_id


@pytest.mark.asyncio
async def test_diagnose_with_request_id():
    """指定 request_id（error_id）→ 精确查询该记录。"""
    seeded = _seed_error(message="target error")

    result = await _call_tool("diagnose_issue", {"request_id": seeded})

    assert result["found"] is True
    assert result["trace_id"] == seeded
    assert result["source"] == "request_id"
    assert result["summary"]["message"] == "target error"


@pytest.mark.asyncio
async def test_diagnose_with_unknown_request_id_returns_not_found():
    """指定不存在的 request_id → found=false + 引导信息，而非空对象。"""
    result = await _call_tool("diagnose_issue", {"request_id": "err-not-exist"})

    assert result["found"] is False
    assert "err-not-exist" in result["message"]
    assert result["setup_hint"]
    assert result["next_step"]


@pytest.mark.asyncio
async def test_diagnose_query_matches_keyword():
    """query 关键词 → 在近期错误中匹配并返回最匹配一条。"""
    _seed_error(message="payment gateway timeout")
    _seed_error(exc_type="AuthError", message="登录失败: token expired")

    result = await _call_tool("diagnose_issue", {"query": "登录"})

    assert result["found"] is True
    assert result["source"] == "query"
    assert "登录" in result["summary"]["message"]


@pytest.mark.asyncio
async def test_diagnose_query_no_match_returns_not_found():
    """query 无匹配 → found=false + setup_hint + next_step，不能只回空列表。"""
    _seed_error(message="unrelated error")

    result = await _call_tool("diagnose_issue", {"query": "绝不存在的关键词xyz"})

    assert result["found"] is False
    assert result["setup_hint"]
    assert result["next_step"]


@pytest.mark.asyncio
async def test_diagnose_no_data_returns_found_false_with_hint():
    """无任何数据 → found=false + setup_hint + next_step（不接受空对象）。"""
    result = await _call_tool("diagnose_issue", {})

    assert result["found"] is False
    assert result["message"]
    assert result["setup_hint"]
    assert result["next_step"]


@pytest.mark.asyncio
async def test_diagnose_invalid_params_return_invalid_params():
    """错误参数 → 协议层 -32602（LLM 自纠错依据）。"""
    for bad_args in (
        {"since_minutes": "abc"},   # 类型错误
        {"query": 123},             # 类型错误
        {"request_id": None},       # 显式 null
    ):
        req = JSONRPCRequest(
            id="diag-bad",
            method="tools/call",
            params={"name": "diagnose_issue", "arguments": bad_args},
        )
        resp = await _handle_tools_call(req)
        assert resp["error"]["code"] == -32602, f"{bad_args} 应返回 -32602"


@pytest.mark.asyncio
async def test_diagnose_session_id_isolation():
    """session_id 隔离查询：A 会话的错误对 B 会话不可见。"""
    _seed_error(message="session A error", session_id="sess-a")

    result_a = await _call_tool("diagnose_issue", {"session_id": "sess-a"})
    result_b = await _call_tool("diagnose_issue", {"session_id": "sess-b"})

    assert result_a["found"] is True
    assert result_b["found"] is False


# ── list_recent_traces / search_logs MCP 工具 ───────────────────────


@pytest.mark.asyncio
async def test_list_recent_traces_tool():
    _seed_error(message="first")
    _seed_error(exc_type="AuthError", message="second")

    result = await _call_tool("list_recent_traces", {"limit": 5})

    assert result["count"] == 2
    assert len(result["traces"]) == 2
    top = result["traces"][0]
    assert top["trace_id"]
    assert top["type"]
    assert "message" in top


@pytest.mark.asyncio
async def test_search_logs_tool():
    _seed_error(exc_type="TimeoutError", message="request timeout after 30s")
    _seed_error(exc_type="AuthError", message="登录失败")

    result = await _call_tool("search_logs", {"keyword": "timeout"})

    assert result["count"] == 1
    assert result["results"][0]["type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_search_logs_session_isolation():
    """FIX(v0.7.3): 带 session_id 搜索不得泄漏其他会话的全局存储摘要。"""
    _seed_error(exc_type="TimeoutError", message="session A timeout", session_id="sess-a")

    own = await _call_tool("search_logs", {"keyword": "timeout", "session_id": "sess-a"})
    other = await _call_tool("search_logs", {"keyword": "timeout", "session_id": "sess-b"})

    assert own["count"] == 1
    assert other["count"] == 0


@pytest.mark.asyncio
async def test_search_logs_missing_keyword_returns_invalid_params():
    """keyword 必填：缺失 → -32602。"""
    req = JSONRPCRequest(
        id="diag-s1",
        method="tools/call",
        params={"name": "search_logs", "arguments": {}},
    )
    resp = await _handle_tools_call(req)
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_diagnose_issue_registered_and_listed():
    """diagnose_issue 已注册且 description 自包含调用策略。"""
    tool = _tool_registry["diagnose_issue"]
    assert "优先" in tool["description"]
    assert "request_id" in tool["description"]

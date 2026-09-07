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


# ── R5：指定 request_id 时会话过滤必须贯穿上下文构建 ──────────────────────


@pytest.mark.asyncio
async def test_diagnose_request_id_wrong_session_not_found():
    """FIX: R5 —— A 会话数据用 B 会话的精确查询不可见（含 debug_context）。"""
    seeded = _seed_error(message="session A only", session_id="sess-a")

    result = await _call_tool("diagnose_issue", {"request_id": seeded, "session_id": "sess-b"})

    assert result["found"] is False
    assert result["setup_hint"]


@pytest.mark.asyncio
async def test_diagnose_request_id_right_session_found_with_context():
    """归属会话的精确查询返回摘要 + 完整上下文。"""
    seeded = _seed_error(message="session A only", session_id="sess-a")

    result = await _call_tool("diagnose_issue", {"request_id": seeded, "session_id": "sess-a"})

    assert result["found"] is True
    assert result["summary"]["message"] == "session A only"
    assert result["debug_context"]["exception"]["message"] == "session A only"


@pytest.mark.asyncio
async def test_diagnose_store_fallback_session_enforced():
    """内存未命中（模拟重启/缓冲淘汰）后，存储回退同样受会话过滤约束。"""
    seeded = _seed_error(message="persisted session A", session_id="sess-a")
    from app.runtime.core import errors as errors_mod

    errors_mod._recent.clear()

    wrong = await _call_tool("diagnose_issue", {"request_id": seeded, "session_id": "sess-b"})
    assert wrong["found"] is False

    right = await _call_tool("diagnose_issue", {"request_id": seeded, "session_id": "sess-a"})
    assert right["found"] is True
    assert right["debug_context"]["exception"]["message"] == "persisted session A"


@pytest.mark.asyncio
async def test_diagnose_query_session_isolated():
    """FIX: R5 —— 关键词查询在错误会话下不可见。"""
    _seed_error(message="login failed in sess A", session_id="sess-a")

    result = await _call_tool("diagnose_issue", {"query": "login", "session_id": "sess-b"})

    assert result["found"] is False


@pytest.mark.asyncio
async def test_diagnose_empty_session_id_treated_as_unspecified():
    """空串 session_id 等价于未指定（不应命中 "" bucket）。"""
    seeded = _seed_error(message="no session filter", session_id=None)

    result = await _call_tool("diagnose_issue", {"request_id": seeded, "session_id": ""})

    assert result["found"] is True


# ── R1 × diagnose：SDK 页面现场经 error_id 诊断可见 ───────────────────────


@pytest.mark.asyncio
async def test_diagnose_error_returns_caller_network_and_ui():
    """FIX: R1 —— 网络/UI 按 caller sdk-trace-ID 上报、错误存于 err-ID：
    宿主拿错误回执 ID 调 diagnose_issue 必须能取回同次现场。"""
    from app.runtime.core.trace_repo import save_network_record, save_ui_event

    caller_tid = "sdk-trace-r1-diagnose"
    save_ui_event(
        {"event_type": "click", "target_selector": "#login", "route_path": "/login"},
        trace_id=caller_tid,
    )
    save_network_record(
        {"method": "POST", "url": "http://x/api/login", "status_code": 500},
        trace_id=caller_tid,
    )
    error_id = save_trace(
        exc_type="Error",
        message="review login failed",
        frames=[{"file": "app.js", "line": 1, "function": "login"}],
        source="browser-sdk",
        trace_id=caller_tid,
    )

    result = await _call_tool("diagnose_issue", {"request_id": error_id})

    assert result["found"] is True
    ctx = result["debug_context"]
    assert ctx.get("network_trace"), "network_trace 不应为空"
    assert ctx["network_trace"][0]["status_code"] == 500
    assert ctx.get("ui_events"), "ui_events 不应为空"
    assert ctx["ui_events"][0]["event_type"] == "click"


@pytest.mark.asyncio
async def test_diagnose_context_filters_caller_events_by_session():
    """R2/R5：同一 caller trace 下的不同会话事件不得混入诊断上下文。"""
    from app.runtime.core.trace_repo import (
        save_console_log,
        save_network_record,
        save_ui_event,
    )

    caller_tid = "sdk-trace-session-isolation"
    error_id = _seed_error(
        message="session A error",
        trace_id=caller_tid,
        session_id="sess-a",
    )
    save_network_record(
        {"method": "GET", "url": "http://x/a"},
        trace_id=caller_tid,
        session_id="sess-a",
    )
    save_network_record(
        {"method": "GET", "url": "http://x/b"},
        trace_id=caller_tid,
        session_id="sess-b",
    )
    save_ui_event({"event_type": "a"}, trace_id=caller_tid, session_id="sess-a")
    save_ui_event({"event_type": "b"}, trace_id=caller_tid, session_id="sess-b")
    save_console_log("error", "console-a", trace_id=caller_tid, session_id="sess-a")
    save_console_log("error", "console-b", trace_id=caller_tid, session_id="sess-b")

    result = await _call_tool(
        "diagnose_issue", {"request_id": error_id, "session_id": "sess-a"}
    )
    assert result["found"] is True
    context = result["debug_context"]
    assert [r["url"] for r in context["network_trace"]] == ["http://x/a"]
    assert [e["event_type"] for e in context["ui_events"]] == ["a"]
    assert [e["message"] for e in context["console_logs"]] == ["console-a"]

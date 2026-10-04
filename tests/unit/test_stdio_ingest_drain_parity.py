"""R6：stdio 传输必须与 HTTP 共用同一排水实现（_lujo_ingest）。

背景（Lead 冻结产物 stdio 冒烟红相）：HTTP 走
app/mcp/protocol/server.py::_handle_tools_call，在结果回包前调用
ingest_dispatch.drain_result_ingest_events(result)；stdio 走
app/mcp_server.py::call_tool -> _run_registered_tool，是另一条并行实现，
此前完全不排水。后果：
1. stdio（npm 默认传输）下 auto_test 截获的现场永不入库，diagnose_issue
   查不到（冻结冒烟：marker1/marker2 queryable=false，且返回的 sdk_capture
   连 events_ingested 字段都没有）；
2. 内部保留键 _lujo_ingest 会随结果被 json.dumps 进 MCP 文本响应
   （内部键泄漏）。

修复契约（本文件固化）：call_tool 在 _run_registered_tool 成功返回后、
tool_failure_predicate / 序列化之前，调用与 HTTP 侧**同一实现**的
drain_result_ingest_events；无该键为 no-op。
"""
from __future__ import annotations

import json

import pytest

from app.config import settings
from app.mcp.tools import ingest_dispatch
from app.mcp.tools.ingest_dispatch import RESULT_INGEST_KEY


@pytest.fixture(autouse=True)
def _fresh_pools(monkeypatch):
    """隔离池状态：与既有 stdio 用例同法注入全新 SlotPool（防整包顺序污染）。"""
    from app.mcp.protocol.executor_lifecycle import SlotPool
    from app.mcp.protocol import server as protocol
    import app.mcp_server as stdio

    light = SlotPool("light", settings.tool_executor_workers)
    heavy = SlotPool("heavy", settings.tool_heavy_executor_workers)
    monkeypatch.setattr(protocol, "_light_pool", light)
    monkeypatch.setattr(protocol, "_heavy_pool", heavy)
    monkeypatch.setattr(stdio, "_light_pool", light)
    monkeypatch.setattr(stdio, "_heavy_pool", heavy)


@pytest.fixture()
def _restore_registry():
    from app.mcp.protocol.server import _tool_registry

    before = dict(_tool_registry)
    yield
    _tool_registry.clear()
    _tool_registry.update(before)


def _install_fake_tool(monkeypatch, name: str, result: dict):
    """注册一个轻量测试工具，并让 stdio 的执行层直接返回 result。"""
    import app.mcp_server as stdio
    from app.mcp.protocol.server import register_tool

    register_tool(name, "R6 排水探针", lambda arguments: {}, inputSchema={})

    async def _fake_run(tool_name, tool, arguments, **kwargs):
        return json.loads(json.dumps(result))  # 深拷贝，避免用例间互相污染

    monkeypatch.setattr(stdio, "_run_registered_tool", _fake_run)


def _spy_on_drain(monkeypatch, calls: list):
    """同时挂 stdio 绑定与 ingest_dispatch 模块属性（两种 import 风格都覆盖）。"""
    import app.mcp_server as stdio

    real = ingest_dispatch.drain_result_ingest_events

    def _spy(result):
        calls.append(dict(result.get("sdk_capture") or {}))
        return real(result)

    monkeypatch.setattr(ingest_dispatch, "drain_result_ingest_events", _spy)
    if hasattr(stdio, "drain_result_ingest_events"):
        monkeypatch.setattr(stdio, "drain_result_ingest_events", _spy)
    return real


@pytest.mark.asyncio
async def test_stdio_call_tool_drains_and_does_not_leak_internal_key(
    monkeypatch, _restore_registry
):
    """R6 红相：stdio 必须排水 _lujo_ingest（入库 + 不泄漏内部键）。"""
    import app.mcp_server as stdio
    from app.mcp.tools.diagnose_api import _enumerate_fault_candidates
    from app.runtime.core import trace_repo

    _install_fake_tool(monkeypatch, "r6_stdio_drain", {
        "url": "http://127.0.0.1:8765/page",
        "sdk_capture": {"enabled": True, "init": "ready", "events_captured": 1},
        RESULT_INGEST_KEY: [
            {
                "path": "/ingest/console",
                "payload": {
                    "level": "error",
                    "message": "STDIO-DRAINMARKER unit",
                    "source": "browser_sdk",
                    "session_id": "sess-stdio-drain",
                },
            },
        ],
    })
    calls: list = []
    _spy_on_drain(monkeypatch, calls)

    result = await stdio.call_tool("r6_stdio_drain", {})

    # 1) 排水必须走与 HTTP 同一实现（且只走一次）
    assert len(calls) == 1, f"stdio 必须调用同一 drain 实现一次：{calls}"
    # 2) 内部键不得泄漏进 MCP 文本响应
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert RESULT_INGEST_KEY not in payload, f"内部键泄漏进 stdio 响应：{payload}"
    assert payload.get("url") == "http://127.0.0.1:8765/page"
    # 3) 真实 drain 语义：events_ingested 写回 sdk_capture
    assert payload["sdk_capture"].get("events_ingested") == 1, payload
    # 4) 事件真的进了可查询存储（diagnose 候选回查 marker）
    candidates, _complete = _enumerate_fault_candidates(None, since_minutes=0)
    found_marker = False
    for cand in candidates:
        for entry in trace_repo.get_console_logs(cand.get("request_id")):
            if "STDIO-DRAINMARKER" in str(entry.get("message") or ""):
                found_marker = True
    assert found_marker, f"stdio 排水后 marker 应可回查：{candidates}"


@pytest.mark.asyncio
async def test_stdio_call_tool_without_ingest_key_skips_drain(
    monkeypatch, _restore_registry
):
    """无内部键时排水为 no-op：不得多调用，也不得凭空造 events_ingested。"""
    import app.mcp_server as stdio

    _install_fake_tool(monkeypatch, "r6_stdio_plain", {
        "url": "http://127.0.0.1:8765/plain",
        "found_elements": 0,
    })
    calls: list = []
    _spy_on_drain(monkeypatch, calls)

    result = await stdio.call_tool("r6_stdio_plain", {})

    assert calls == [], f"无 _lujo_ingest 时不得调用排水：{calls}"
    payload = json.loads(result[0].text)
    assert payload == {"url": "http://127.0.0.1:8765/plain", "found_elements": 0}, payload

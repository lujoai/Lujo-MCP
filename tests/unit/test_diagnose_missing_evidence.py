"""diagnose_issue 集成：missing_evidence 证据缺口提示（新增可选字段）。

覆盖：
1. 最简现场（只有堆栈，无 network/UI/git/spec）→ missing_evidence 含对应缺口
2. 完整现场（各维度证据齐备）→ missing_evidence 为 None
3. fail-open：缺口计算抛异常 / scorer 抛异常时 diagnose_issue 仍正常返回

字段由 diagnose_issue 在返回的调试上下文 dict 上注入（不扩展
build_debug_context 的输出字段契约）：无缺失时为 None，不影响任何既有
字段与断言。
"""
import json
from types import SimpleNamespace

import pytest

from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call
from app.mcp.tools import register_all_tools
from app.runtime.core.trace_repo import save_trace


@pytest.fixture(autouse=True)
def _registered_tools():
    register_all_tools()
    # 与 test_diagnose_issue.py 同口径：每个用例前重置为全新 memory 后端
    from app.runtime.core.storage import factory as _storage_factory

    _storage_factory._trace_store = None
    yield
    _storage_factory._trace_store = None


async def _diagnose(arguments: dict) -> dict:
    req = JSONRPCRequest(
        id="me-1",
        method="tools/call",
        params={"name": "diagnose_issue", "arguments": arguments},
    )
    resp = await _handle_tools_call(req)
    assert resp.get("error") is None, f"协议层报错: {resp.get('error')}"
    return json.loads(resp["result"]["content"][0]["text"])


# ── 1. 最简现场：只有堆栈 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_minimal_scene_reports_missing_evidence():
    """只有堆栈、无 network/UI/git/spec → 缺口逐项列出且文案可执行。"""
    error_id = save_trace(
        exc_type="TypeError",
        message="Cannot read properties of undefined (reading 'token')",
        frames=[{"file": "src/login.js", "line": 42, "function": "handleSubmit"}],
        source="browser-sdk",
        trace_kind="exception",
        trace_id="sdk-trace-me-min",
    )

    result = await _diagnose({"request_id": error_id})

    assert result["found"] is True
    ctx = result["debug_context"]
    missing = ctx["missing_evidence"]
    assert isinstance(missing, list) and missing
    dims = {m["dimension"] for m in missing}
    # 最简现场下这些维度必然无真实证据
    # （spec 不强求：判定基于真实证据，测试从仓库根运行时
    # get_related_specs 会扫到本仓库自身 docs 规范文件，属环境相关）
    assert {"network", "ui_event", "git_context"} <= dims
    # 有堆栈 → trace 不应报缺
    assert "trace" not in dims
    for m in missing:
        assert set(m.keys()) == {"dimension", "hint"}
        assert m["hint"].strip()
    # 缺口的提示必须指向真实可调用的工具
    network_hint = next(m["hint"] for m in missing if m["dimension"] == "network")
    assert "get_network_trace" in network_hint


# ── 2. 完整现场：各维度证据齐备 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_full_scene_missing_evidence_is_none(monkeypatch):
    """network/UI/git/spec/源码齐备 → missing_evidence 为 None（无缺口）。"""
    from app.runtime.core import git as git_mod
    from app.runtime.core.logs import add_log
    from app.runtime.core.trace_repo import save_network_record, save_ui_event

    caller_tid = "sdk-trace-me-full"
    save_network_record(
        {"method": "POST", "url": "http://x/api/login", "status_code": 500},
        trace_id=caller_tid,
    )
    save_ui_event(
        {"event_type": "click", "target_selector": "#login"},
        trace_id=caller_tid,
    )
    error_id = save_trace(
        exc_type="Error",
        message="full scene",
        frames=[{"file": "src/login.js", "line": 42, "function": "handleSubmit"}],
        source="browser-sdk",
        trace_kind="exception",
        trace_id=caller_tid,
    )

    # git 归因：builder 每次调用时才 import，patch 源模块即可（免真实 git 子进程）
    monkeypatch.setattr(
        git_mod,
        "get_blame_for_frame",
        lambda file, line: {"file": file, "line": line, "author": "tester"},
    )
    monkeypatch.setattr(
        git_mod,
        "get_recent_diff",
        lambda file, commits_back=3: {"file": file, "diff": "-x\n+y"},
    )

    # 源码片段命中：patch code_locator 返回 found=True 条目
    import app.runtime.collectors.code_locator as cl_mod

    def _fake_snippets(frames):
        return [
            SimpleNamespace(
                model_dump=lambda: {"file": "src/login.js", "found": True, "snippet": "x = 1"}
            )
        ]

    monkeypatch.setattr(cl_mod, "get_snippets_for_frames", _fake_snippets)

    # 规范校验结果：verify 条目直接写在 error_id 下（builder 按 get_logs(tid) 读取）
    add_log(
        error_id,
        "verify",
        {"matched": False, "diffs": [{"field": "status", "expected": 200, "actual": 500}]},
    )

    result = await _diagnose({"request_id": error_id})

    assert result["found"] is True
    ctx = result["debug_context"]
    # 完整现场：字段存在但无缺口（None；JSON 序列化为 null）
    assert ctx["missing_evidence"] is None
    # 反向核对：证据维度确实齐备（防测试自身失效）
    assert ctx["network_trace"]
    assert ctx["ui_events"]
    assert ctx["git_blame"]
    assert ctx["recent_diffs"]
    assert ctx["spec_diffs"]
    assert all(s.get("found") for s in ctx["code_snippets"])


# ── 3. fail-open：提示/评分逻辑抛异常不得拖垮 diagnose_issue ─────────


@pytest.mark.asyncio
async def test_diagnose_survives_evidence_gap_computation_failure(monkeypatch):
    """缺口计算抛异常 → 字段降级为 None，diagnose_issue 整体正常返回。"""
    import app.runtime.context.evidence_gaps as eg_mod

    error_id = save_trace(
        exc_type="ValueError",
        message="boom",
        frames=[{"file": "src/a.js", "line": 1, "function": "f"}],
        source="browser-sdk",
        trace_kind="exception",
        trace_id="sdk-trace-me-failopen",
    )

    def _boom(_ctx):
        raise RuntimeError("simulated evidence-gap failure")

    monkeypatch.setattr(eg_mod, "compute_missing_evidence", _boom)

    result = await _diagnose({"request_id": error_id})

    assert result["found"] is True
    ctx = result["debug_context"]
    assert ctx["missing_evidence"] is None
    # 主体现场不受影响
    assert ctx["exception"]["message"] == "boom"


@pytest.mark.asyncio
async def test_diagnose_survives_scorer_failure(monkeypatch):
    """scorer 抛异常 → diagnose_issue 仍正常返回（评分链路与诊断链路解耦）。"""
    import app.quality.scorer as scorer_mod

    error_id = save_trace(
        exc_type="ValueError",
        message="boom with broken scorer",
        frames=[{"file": "src/b.js", "line": 2, "function": "g"}],
        source="browser-sdk",
        trace_kind="exception",
        trace_id="sdk-trace-me-scorer",
    )

    def _boom(_ctx):
        raise RuntimeError("simulated scorer failure")

    monkeypatch.setattr(scorer_mod, "evaluate", _boom)

    result = await _diagnose({"request_id": error_id})

    assert result["found"] is True
    assert result["debug_context"]["exception"]["message"] == "boom with broken scorer"

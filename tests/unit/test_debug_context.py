"""build_debug_context 单测：验证完整上下文注入（code_snippets/git/network/ui/runtime）"""
import pytest

from app.config import settings
from app.runtime.core import trace_repo
from app.runtime.context.builder import build_debug_context
from app.mcp.tools import network_api


@pytest.fixture(autouse=True)
def _redaction_on():
    saved = settings.redaction_enabled
    settings.redaction_enabled = True
    yield
    settings.redaction_enabled = saved


def test_build_debug_context_returns_none_for_missing():
    assert build_debug_context("no-such-trace") is None


def test_build_debug_context_injects_all_sections():
    # 用真实已跟踪文件，保证 code_locator 能读到源码、git blame 可命中
    tid = trace_repo.save_trace(
        "ValueError", "bad value",
        frames=[{"file": "app/config.py", "line": 9, "function": "Settings"}],
        source="test",
    )
    # 关联网络与 UI（经采集器，贴近真实路径）
    network_api.tool_ingest_network(
        {"method": "get", "url": "http://x/api", "status_code": 200}, trace_id=tid
    )
    trace_repo.save_ui_event({"event_type": "click", "target_selector": "#btn"}, trace_id=tid)

    ctx = build_debug_context(tid)

    assert ctx is not None
    assert ctx["trace_id"] == tid
    assert ctx["trace_kind"] == "exception"
    # 兼容 analyzer 的字段
    assert ctx["exception"]["type"] == "ValueError"
    assert ctx["exception"]["frames"][0]["file"] == "app/config.py"
    assert ctx["runtime"] is not None
    # 源码片段命中
    assert ctx["code_snippets"]
    assert ctx["code_snippets"][0]["found"] is True
    # 网络链 / UI 事件注入
    assert ctx["network_trace"] is not None
    assert ctx["network_trace"][0]["method"] == "GET"
    assert ctx["ui_events"] is not None
    assert ctx["ui_events"][0]["event_type"] == "click"
    # git 归因字段存在（可能为 None/list，取决于 git 可用性）
    assert "git_blame" in ctx
    assert "recent_diffs" in ctx


def test_build_debug_context_without_runtime():
    tid = trace_repo.save_trace("E", "m", [])
    ctx = build_debug_context(tid, include_runtime=False)
    assert ctx is not None
    assert ctx["runtime"] is None


def test_build_debug_context_silent_failure_trace_kind():
    tid = trace_repo.save_trace("SilentFailure", "no response", [], trace_kind="silent_failure")
    ctx = build_debug_context(tid)
    assert ctx["trace_kind"] == "silent_failure"


def test_build_debug_context_redacts_network_and_ui():
    tid = trace_repo.save_trace("E", "m", [])
    trace_repo.save_network_record({"url": "http://x/?token=secret"}, trace_id=tid)
    trace_repo.save_ui_event({"payload_json": 'password = "pw"'}, trace_id=tid)
    ctx = build_debug_context(tid)
    assert "secret" not in ctx["network_trace"][0]["url"]
    assert "pw" not in ctx["ui_events"][0]["payload_json"]


def test_build_debug_context_includes_related_specs_key():
    """build_debug_context 必须含 related_specs 字段（M9b 注入）。"""
    tid = trace_repo.save_trace(
        "ValueError", "m",
        frames=[{"file": "app/config.py", "line": 1, "function": "Settings"}],
    )
    ctx = build_debug_context(tid)
    assert "related_specs" in ctx
    # 值为 list 或 None（取决于项目是否存在匹配规范）
    assert ctx["related_specs"] is None or isinstance(ctx["related_specs"], list)


def test_build_debug_context_injects_spec_diffs():
    """verify 结果（spec_diffs）注入 build_debug_context（V5 闭环）。"""
    from app.mcp.tools.verify_api import verify_handler

    tid = trace_repo.save_trace("E", "m", [])
    # 对该 trace 做 verify，结果会持久化
    verify_handler({
        "actual": {"status_code": 200, "body": {"name": "Bob"}},
        "spec": {"kind": "api", "expect": {"body_rules": {"name": "Alice"}}},
        "trace_id": tid,
    })

    ctx = build_debug_context(tid)
    assert ctx is not None
    assert ctx["spec_diffs"] is not None
    assert len(ctx["spec_diffs"]) == 1
    assert ctx["spec_diffs"][0]["matched"] is False
    assert ctx["spec_diffs"][0]["silent_failure"] is True


def test_build_debug_context_spec_diffs_none_when_no_verify():
    """无 verify 结果时 spec_diffs=None。"""
    tid = trace_repo.save_trace("E", "m", [])
    ctx = build_debug_context(tid)
    assert ctx is not None
    assert ctx["spec_diffs"] is None

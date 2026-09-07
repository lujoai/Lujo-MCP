"""trace_repo 统一存取层单测"""
import pytest
from unittest.mock import patch

from app.config import settings
from app.runtime.core import trace_repo
from app.runtime.core import errors
from app.runtime.core.logs import list_request_ids, get_logs


@pytest.fixture(autouse=True)
def _redaction_on():
    saved = settings.redaction_enabled
    settings.redaction_enabled = True
    yield
    settings.redaction_enabled = saved


def test_save_and_get_trace():
    frames = [{"file": "a.py", "line": 10, "function": "f"}]
    tid = trace_repo.save_trace("ValueError", "bad value", frames, source="ingest")
    assert tid

    got = trace_repo.get_trace(tid)
    assert got is not None
    assert got["trace_id"] == tid
    assert got["exc_type"] == "ValueError"
    assert got["message"] == "bad value"
    assert got["frames"] == frames
    assert got["trace_kind"] == "exception"
    assert got["source"] == "ingest"


def test_trace_kind_and_extra_persisted():
    tid = trace_repo.save_trace(
        "SilentFailure", "click no response", [],
        source="browser_sdk", extra={"expectation": "route_change"},
        trace_kind="silent_failure",
    )
    got = trace_repo.get_trace(tid)
    assert got["trace_kind"] == "silent_failure"
    assert got["extra"] == {"expectation": "route_change"}


def test_get_trace_none_when_missing():
    assert trace_repo.get_trace("does-not-exist") is None


def test_network_record_save_and_get():
    tid = trace_repo.save_trace("E", "m", [])
    rid = trace_repo.save_network_record(
        {"method": "GET", "url": "http://x/api", "status_code": 200, "duration_ms": 12.5},
        trace_id=tid,
    )
    records = trace_repo.get_network_records(tid)
    assert len(records) == 1
    assert records[0]["record_id"] == rid
    assert records[0]["method"] == "GET"
    assert records[0]["trace_id"] == tid
    assert records[0]["direction"] == "outbound"


def test_network_redaction_at_storage_boundary():
    tid = trace_repo.save_trace("E", "m", [])
    trace_repo.save_network_record(
        {"url": "http://x/?token=secret", "request_body": 'password = "pw"', "response_body": "ok"},
        trace_id=tid,
    )
    rec = trace_repo.get_network_records(tid)[0]
    assert "secret" not in rec["url"]
    assert "pw" not in rec["request_body"]
    assert rec["response_body"] == "ok"


def test_ui_event_save_and_get():
    tid = trace_repo.save_trace("E", "m", [])
    eid = trace_repo.save_ui_event(
        {"event_type": "click", "target_selector": "#btn", "route_path": "/page"},
        trace_id=tid,
    )
    events = trace_repo.get_ui_events(tid)
    assert len(events) == 1
    assert events[0]["event_id"] == eid
    assert events[0]["event_type"] == "click"
    assert events[0]["trace_id"] == tid


def test_ui_event_redaction_at_storage_boundary():
    tid = trace_repo.save_trace("E", "m", [])
    trace_repo.save_ui_event(
        {"event_type": "submit", "payload_json": 'password = "pw"'},
        trace_id=tid,
    )
    ev = trace_repo.get_ui_events(tid)[0]
    assert "pw" not in ev["payload_json"]


def test_network_and_ui_isolated_by_step():
    """同一 trace_id 下 network 与 ui_event 互不混入。"""
    tid = trace_repo.save_trace("E", "m", [])
    trace_repo.save_network_record({"url": "http://a"}, trace_id=tid)
    trace_repo.save_ui_event({"event_type": "click"}, trace_id=tid)
    assert len(trace_repo.get_network_records(tid)) == 1
    assert len(trace_repo.get_ui_events(tid)) == 1


def test_event_records_are_isolated_by_session():
    """网络/UI/console 事件带 session_id 时按会话 fail-closed 过滤。"""
    tid = trace_repo.save_trace("E", "m", [], session_id="session-a")
    trace_repo.save_network_record({"url": "http://a"}, trace_id=tid, session_id="session-a")
    trace_repo.save_network_record({"url": "http://b"}, trace_id=tid, session_id="session-b")
    trace_repo.save_ui_event({"event_type": "click-a"}, trace_id=tid, session_id="session-a")
    trace_repo.save_ui_event({"event_type": "click-b"}, trace_id=tid, session_id="session-b")
    trace_repo.save_console_log("error", "a", trace_id=tid, session_id="session-a")
    trace_repo.save_console_log("error", "b", trace_id=tid, session_id="session-b")

    assert [r["url"] for r in trace_repo.get_network_records(tid, session_id="session-a")] == ["http://a"]
    assert [r["url"] for r in trace_repo.get_network_records(tid, session_id="session-b")] == ["http://b"]
    assert [e["event_type"] for e in trace_repo.get_ui_events(tid, session_id="session-a")] == ["click-a"]
    assert [e["event_type"] for e in trace_repo.get_ui_events(tid, session_id="session-b")] == ["click-b"]
    assert [e["message"] for e in trace_repo.get_console_logs(tid, session_id="session-a")] == ["a"]
    assert [e["message"] for e in trace_repo.get_console_logs(tid, session_id="session-b")] == ["b"]
    # 旧的无会话查询保持兼容，仍可查看同一 trace 下的全部事件。
    assert len(trace_repo.get_network_records(tid)) == 2


def test_save_trace_redacts_frames_before_storage():
    frames = [
        {
            "file": "auth.py",
            "line": 10,
            "function": "login",
            "locals": {
                "token": "abc123",
                "message": 'api_key = "sk-secret"',
            },
        }
    ]
    tid = trace_repo.save_trace("AuthError", "boom", frames, extra={"authorization": "Bearer xyz"})
    got = trace_repo.get_trace(tid)
    assert got is not None
    stored_locals = got["frames"][0]["locals"]
    assert "abc123" not in str(stored_locals)
    assert "sk-secret" not in str(stored_locals)
    assert got["extra"]["authorization"] != "Bearer xyz"


# ── C3：返回 ID 与 add_log 写入 key 统一 ──

def test_save_trace_returns_id_matching_add_log_key():
    """save_trace 返回值必须在 list_request_ids 中（add_log key == 返回值）"""
    error_id = trace_repo.save_trace("ValueError", "msg", [{"file": "a.py", "line": 1, "function": "f"}])
    # 返回值应为 error_id（err- 前缀）
    assert error_id.startswith("err-")
    # error_id 必须在 trace_store 的 list_request_ids 中（add_log 写入成功）
    assert error_id in list_request_ids(limit=200)


def test_save_trace_with_caller_trace_id_returns_error_id():
    """C3 SDK 场景：传 trace_id="sdk-abc"，返回值必须是 error_id，不是 sdk-abc"""
    caller_tid = "sdk-abc-123"
    error_id = trace_repo.save_trace(
        "SilentFailure", "click no response", [],
        source="browser_sdk", trace_kind="silent_failure",
        trace_id=caller_tid,
    )
    # 返回值必须是 error_id，而不是 caller_trace_id
    assert error_id != caller_tid
    assert error_id.startswith("err-")
    # add_log key 必须是 error_id（trace_data / trace_meta / trace_link 都应在 error_id 下）
    steps = [e["step"] for e in get_logs(error_id)]
    assert "trace_data" in steps
    assert "trace_meta" in steps
    assert "trace_link" in steps
    # caller_trace_id 不应作为 add_log key
    assert caller_tid not in list_request_ids(limit=200)


def test_save_trace_with_caller_trace_id_records_link_under_error_id():
    """C3：trace_link 必须写在 error_id 下，且记录 caller_trace_id"""
    caller_tid = "sdk-xyz-789"
    error_id = trace_repo.save_trace("E", "m", [], trace_id=caller_tid)
    # 在 error_id 下找 trace_link 条目
    link_entries = [e for e in get_logs(error_id) if e.get("step") == "trace_link"]
    assert len(link_entries) == 1
    link_data = link_entries[0].get("data") or {}
    assert link_data.get("caller_trace_id") == caller_tid
    # get_trace 返回值应暴露 caller_trace_id 供审计
    got = trace_repo.get_trace(error_id)
    assert got is not None
    assert got.get("caller_trace_id") == caller_tid


# ── C4：PG 持久化与回读链路 ──

def test_save_trace_persists_error_data_to_trace_store():
    """C4 上半段：save_trace 必须把完整异常数据通过 add_log 落到 trace_store"""
    frames = [{"file": "a.py", "line": 10, "function": "f"}]
    error_id = trace_repo.save_trace("ValueError", "bad value", frames, source="ingest")
    # 在 trace_store 中应能找到 step=trace_data 的条目
    data_entries = [e for e in get_logs(error_id) if e.get("step") == "trace_data"]
    assert len(data_entries) == 1
    data = data_entries[0].get("data") or {}
    assert data["type"] == "ValueError"
    assert data["message"] == "bad value"
    assert data["frames"] == frames
    assert data["frame_count"] == 1
    assert data["source"] == "ingest"


def test_get_trace_rebuilds_from_trace_store_when_errors_miss():
    """C4 下半段：errors 内存未命中时，get_trace 从 trace_store 回读重建 trace 对象"""
    frames = [{"file": "a.py", "line": 10, "function": "f"}]
    error_id = trace_repo.save_trace(
        "SilentFailure", "click no response", frames,
        source="browser_sdk", extra={"expectation": "route_change"},
        trace_kind="silent_failure",
    )
    # 模拟重启：清空 errors 内存缓冲（trace_store 保留）
    errors._recent.clear()
    # get_trace 应能从 trace_store 回读重建
    got = trace_repo.get_trace(error_id)
    assert got is not None
    assert got["trace_id"] == error_id
    assert got["exc_type"] == "SilentFailure"
    assert got["message"] == "click no response"
    assert got["frames"] == frames
    assert got["frame_count"] == 1
    assert got["trace_kind"] == "silent_failure"
    assert got["extra"] == {"expectation": "route_change"}
    assert got.get("from_store") is True


def test_get_trace_returns_none_when_neither_errors_nor_store_has_it():
    """errors 与 trace_store 都没有时返回 None"""
    errors._recent.clear()
    assert trace_repo.get_trace("does-not-exist-anywhere") is None


# ── SEC-13：save_trace 写入顺序原子性（commit-marker 模式）──

class TestSaveTraceAtomicity:
    """验证 save_trace 采用 META → LINK → DATA 写入顺序，DATA 作为提交标记。"""

    def test_save_trace_writes_data_last_as_commit_marker(self):
        """带 trace_id 调用 save_trace 时，写入顺序中 _STEP_DATA 必须是最后一步。

        期望顺序：META → LINK → DATA。
        META+LINK 通过 add_logs_batch 批量写入，DATA 通过 add_log 单独写入。
        """
        caller_tid = "sdk-sec13-001"
        with patch("app.runtime.core.trace_repo.add_log") as mock_add_log, \
             patch("app.runtime.core.trace_repo.add_logs_batch") as mock_batch:
            trace_repo.save_trace(
                "SilentFailure", "click no response", [],
                source="browser_sdk", trace_kind="silent_failure",
                trace_id=caller_tid,
            )

        steps = []
        # add_logs_batch 调用：args[1] 是 items 列表 [(step, data), ...]
        for call in mock_batch.call_args_list:
            items = call.args[1]
            steps.extend(item[0] for item in items)
        # add_log 调用：args[1] 是 step
        for call in mock_add_log.call_args_list:
            steps.append(call.args[1])

        assert steps, "应至少有一次写入调用"
        assert steps[-1] == "trace_data", f"DATA 应为最后写入步骤，实际顺序: {steps}"
        assert steps == ["trace_meta", "trace_link", "trace_data"], f"期望顺序 META→LINK→DATA，实际: {steps}"

    def test_save_trace_data_present_implies_meta_present(self):
        """正常调用 save_trace 后，trace_data 与 trace_meta 条目应同时存在。"""
        error_id = trace_repo.save_trace(
            "ValueError", "boom", [{"file": "a.py", "line": 1, "function": "f"}],
            source="ingest", extra={"k": "v"},
        )
        steps = [e["step"] for e in get_logs(error_id)]
        assert "trace_data" in steps, "trace_data 条目应存在"
        assert "trace_meta" in steps, "trace_meta 条目应存在"


# ── R1：caller 别名关联（network / UI / console 经 error_id 可查）──────────

class TestCallerAliasResolution:
    """SDK 真实链路：异常存于 err-...，network/UI/console 按 sdk-trace-... 存储。

    FIX: R1 —— 查询侧必须在存取层统一解析别名，否则宿主拿到错误回执的
    error_id 后取不到同次现场的网络/点击/控制台记录。
    """

    def _seed(self, caller_tid: str, session_id: str | None = None) -> str:
        return trace_repo.save_trace(
            "Error", "boom", [{"file": "app.js", "line": 1, "function": "f"}],
            source="browser-sdk", trace_id=caller_tid, session_id=session_id,
        )

    def test_events_under_caller_visible_via_error_id(self):
        """事件先到（caller 键）、错误后到：经 error_id 可查全部现场。"""
        caller_tid = "sdk-trace-r1-events-first"
        error_id = self._seed(caller_tid)
        trace_repo.save_network_record(
            {"method": "POST", "url": "http://x/api", "status_code": 500}, trace_id=caller_tid
        )
        trace_repo.save_ui_event(
            {"event_type": "click", "target_selector": "#btn"}, trace_id=caller_tid
        )
        trace_repo.save_console_log(level="error", message="boom", trace_id=caller_tid)

        assert len(trace_repo.get_network_records(error_id)) == 1
        assert len(trace_repo.get_ui_events(error_id)) == 1
        assert len(trace_repo.get_console_logs(error_id)) == 1
        # 直接按 caller ID 查询行为不变
        assert len(trace_repo.get_network_records(caller_tid)) == 1

    def test_error_first_events_later_still_resolved(self):
        """错误先到、事件后到（反向到达）：别名解析在查询侧，同样可查。"""
        caller_tid = "sdk-trace-r1-error-first"
        error_id = self._seed(caller_tid)
        trace_repo.save_network_record({"url": "http://x/late"}, trace_id=caller_tid)
        assert len(trace_repo.get_network_records(error_id)) == 1

    def test_alias_does_not_mix_other_pages(self):
        """另一个页面（不同 caller ID）的数据不混入。"""
        error_id = self._seed("sdk-trace-page-a")
        trace_repo.save_network_record({"url": "http://x/a"}, trace_id="sdk-trace-page-a")
        trace_repo.save_network_record({"url": "http://x/b"}, trace_id="sdk-trace-page-b")

        records = trace_repo.get_network_records(error_id)
        assert len(records) == 1
        assert records[0]["url"] == "http://x/a"

    def test_merged_records_sorted_by_timestamp(self):
        """跨别名合并后按时间有序（caller 键旧事件在前）。"""
        caller_tid = "sdk-trace-r1-order"
        error_id = self._seed(caller_tid)
        trace_repo.save_network_record({"url": "http://x/old", "timestamp": 100.0}, trace_id=caller_tid)
        trace_repo.save_network_record({"url": "http://x/new", "timestamp": 200.0}, trace_id=error_id)

        records = trace_repo.get_network_records(error_id)
        assert [r["url"] for r in records] == ["http://x/old", "http://x/new"]


# ── R5：存储回退的会话归属校验 ────────────────────────────────────────────

class TestStoreFallbackSessionEnforcement:
    """FIX: R5 —— errors 内存未命中（重启/淘汰）后走存储回退时，
    会话过滤必须同样生效，否则内存命中与回退行为分叉、隔离被绕过。"""

    def test_rebuild_without_session_still_works(self):
        """不带会话过滤的回读不受影响（向后兼容）。"""
        error_id = trace_repo.save_trace(
            "ValueError", "persisted", [{"file": "a.py", "line": 1, "function": "f"}],
            session_id="sess-a",
        )
        errors._recent.clear()
        got = trace_repo.get_trace(error_id)
        assert got is not None
        assert got["message"] == "persisted"

    def test_rebuild_rejects_foreign_session(self):
        """A 会话数据在 B 会话查询的存储回退中不可见。"""
        error_id = trace_repo.save_trace(
            "ValueError", "sess-a only", [], session_id="sess-a"
        )
        errors._recent.clear()
        assert trace_repo.get_trace(error_id, session_id="sess-b") is None
        assert trace_repo.get_trace(error_id, session_id="sess-a") is not None

    def test_rebuild_sessionless_record_invisible_to_session_query(self):
        """无会话上报的记录对指定会话的查询不可见（fail-closed）。"""
        error_id = trace_repo.save_trace("ValueError", "no session", [])
        errors._recent.clear()
        assert trace_repo.get_trace(error_id, session_id="sess-a") is None
        assert trace_repo.get_trace(error_id) is not None

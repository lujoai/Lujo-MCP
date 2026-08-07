"""silent_failure 工具 + inbound 网络采集中间件单测"""
import pytest

from app.config import settings
from app.mcp.tools import silent_failure_api
from app.runtime.core import trace_repo


@pytest.fixture(autouse=True)
def _redaction_on():
    saved = settings.redaction_enabled
    settings.redaction_enabled = True
    yield
    settings.redaction_enabled = saved


def test_ingest_silent_failure_persists_trace_and_links():
    res = silent_failure_api.tool_ingest_silent_failure(
        message="点击 #submit 后未跳转",
        frames=[{"file": "App.tsx", "line": 42, "function": "onClick"}],
        ui_events=[
            {"event_type": "click", "target_selector": "#submit", "route_path": "/page"},
            {"event_type": "route_change", "route_path": "/page"},
        ],
        network_records=[
            {"method": "post", "url": "http://x/api/submit", "status_code": 200, "duration_ms": 30},
        ],
        expectation={"type": "route_change", "to": "/done", "withinMs": 1000},
        source="browser_sdk",
    )
    assert res["saved"] is True
    assert res["ui_events"] == 2
    assert res["network_records"] == 1
    tid = res["trace_id"]

    # trace_kind 与 extra 落库
    trace = trace_repo.get_trace(tid)
    assert trace["trace_kind"] == "silent_failure"
    assert trace["exc_type"] == "SilentFailure"
    assert trace["extra"]["expectation"]["to"] == "/done"

    # UI 事件链关联
    events = trace_repo.get_ui_events(tid)
    assert len(events) == 2
    assert events[0]["event_type"] == "click"

    # 网络请求链关联
    records = trace_repo.get_network_records(tid)
    assert len(records) == 1
    assert records[0]["method"] == "POST"


def test_ingest_silent_failure_redacts_linked_records():
    res = silent_failure_api.tool_ingest_silent_failure(
        message="silent",
        ui_events=[{"event_type": "click", "payload_json": 'password = "pw"'}],
        network_records=[{"url": "http://x/?token=secret"}],
    )
    tid = res["trace_id"]
    assert "pw" not in trace_repo.get_ui_events(tid)[0]["payload_json"]
    assert "secret" not in trace_repo.get_network_records(tid)[0]["url"]


def test_ingest_silent_failure_drops_invalid_frames():
    res = silent_failure_api.tool_ingest_silent_failure(
        message="m",
        frames=[{"file": "ok.py", "line": 1}, {"no_file": True}, {"file": "x", "line": "bad"}],
    )
    trace = trace_repo.get_trace(res["trace_id"])
    assert len(trace["frames"]) == 1
    assert trace["frames"][0]["file"] == "ok.py"


def test_ingest_silent_failure_minimal_message_only():
    res = silent_failure_api.tool_ingest_silent_failure(message="just a message")
    assert res["saved"] is True
    assert res["ui_events"] == 0
    assert res["network_records"] == 0
    assert res["observed_event_count"] == 0
    assert res["observed_event_merged_count"] == 0
    assert res["observed_event_unknown_count"] == 0
    trace = trace_repo.get_trace(res["trace_id"])
    assert trace["trace_kind"] == "silent_failure"


# ── H10: observed 文本 + observed_events 事件链 ──
def test_ingest_silent_failure_persists_observed_text():
    """SDK 上报的 observed 字符串描述必须写入 trace.extra.observed。"""
    res = silent_failure_api.tool_ingest_silent_failure(
        message="点击 #submit 后未跳转",
        observed="点击后无反应",
        expectation={"type": "route_change", "to": "/done"},
    )
    trace = trace_repo.get_trace(res["trace_id"])
    assert trace["extra"]["observed"] == "点击后无反应"
    assert trace["extra"]["expectation"]["to"] == "/done"
    # 未传 observed_events 时，计数为 0
    assert trace["extra"]["observed_event_count"] == 0
    assert trace["extra"]["observed_event_merged_count"] == 0
    assert trace["extra"]["observed_event_unknown_count"] == 0


def test_ingest_silent_failure_persists_observed_events_chain():
    """observed_events 按 kind 分流入库后，network_trace / ui_events 应能取回。"""
    res = silent_failure_api.tool_ingest_silent_failure(
        message="提交后页面未更新",
        observed="按钮变灰但数据没提交",
        observed_events=[
            {
                "kind": "network",
                "data": {
                    "method": "POST",
                    "url": "http://x/api/submit",
                    "status_code": 200,
                    "duration_ms": 30.5,
                    "request_body": '{"name":"demo"}',
                    "response_body": '{"ok":true}',
                },
            },
            {
                "kind": "ui",
                "data": {
                    "event_type": "click",
                    "target_selector": "#submit",
                    "route_path": "/page",
                    "timestamp": 1784425242.0,
                },
            },
        ],
    )
    assert res["observed_event_count"] == 2
    assert res["observed_event_merged_count"] == 2
    assert res["observed_event_unknown_count"] == 0
    tid = res["trace_id"]

    trace = trace_repo.get_trace(tid)
    assert trace["extra"]["observed"] == "按钮变灰但数据没提交"
    assert trace["extra"]["observed_event_count"] == 2
    assert trace["extra"]["observed_event_merged_count"] == 2

    # network 事件入库
    records = trace_repo.get_network_records(tid)
    assert len(records) == 1
    assert records[0]["method"] == "POST"
    assert records[0]["url"] == "http://x/api/submit"
    assert records[0]["status_code"] == 200

    # UI 事件入库
    events = trace_repo.get_ui_events(tid)
    assert len(events) == 1
    assert events[0]["event_type"] == "click"
    assert events[0]["target_selector"] == "#submit"


def test_ingest_silent_failure_observed_events_redacted():
    """observed_events 入库前必须脱敏。

    - network 事件 data.request_body 含 JSON password 字段 → 入库 request_body 应脱敏
    - UI 事件 data.payload_json 含 token=xxx 字符串 → 入库 payload_json 应脱敏
    """
    res = silent_failure_api.tool_ingest_silent_failure(
        message="silent",
        observed_events=[
            {
                "kind": "network",
                "data": {
                    "method": "POST",
                    "url": "http://x/api/login",
                    "request_body": '{"password":"pw-secret","user":"demo"}',
                    "status_code": 200,
                },
            },
            {
                "kind": "ui",
                "data": {
                    "event_type": "click",
                    "payload_json": "token=tok-xyz&action=submit",
                },
            },
        ],
    )
    tid = res["trace_id"]

    records = trace_repo.get_network_records(tid)
    assert len(records) == 1
    assert "pw-secret" not in records[0]["request_body"]
    assert "***" in records[0]["request_body"]

    events = trace_repo.get_ui_events(tid)
    assert len(events) == 1
    assert "tok-xyz" not in events[0]["payload_json"]
    assert "***" in events[0]["payload_json"]


def test_ingest_silent_failure_observed_events_unknown_preserved():
    """无法识别 kind 的事件不丢弃，保留到 extra.observed_events_unknown。"""
    res = silent_failure_api.tool_ingest_silent_failure(
        message="mixed events",
        observed_events=[
            {"kind": "network", "data": {"method": "GET", "url": "http://x/a"}},
            {"kind": "ui", "data": {"event_type": "click"}},
            {"kind": "unknown_kind", "data": {"foo": "bar"}},
            {"no_kind": True},
            "not_a_dict",
        ],
    )
    assert res["observed_event_count"] == 5
    assert res["observed_event_merged_count"] == 2  # 只有 1 network + 1 ui 能识别
    assert res["observed_event_unknown_count"] == 3

    trace = trace_repo.get_trace(res["trace_id"])
    assert trace["extra"]["observed_event_unknown_count"] == 3
    unknown = trace["extra"]["observed_events_unknown"]
    assert len(unknown) == 3
    # 原始数据完整保留，便于 AI 调试
    assert {"kind": "unknown_kind", "data": {"foo": "bar"}} in unknown
    assert {"no_kind": True} in unknown
    assert {"raw": "not_a_dict"} in unknown


def test_ingest_silent_failure_observed_events_merges_with_external_records():
    """observed_events 与外部 network_records/ui_events 合并入库，数量记录在 extra。"""
    res = silent_failure_api.tool_ingest_silent_failure(
        message="both sources",
        network_records=[{"method": "GET", "url": "http://x/ext"}],
        ui_events=[{"event_type": "input", "route_path": "/page"}],
        observed_events=[
            {"kind": "network", "data": {"method": "POST", "url": "http://x/obs"}},
            {"kind": "ui", "data": {"event_type": "click"}},
        ],
    )
    assert res["network_records"] == 2  # 1 external + 1 from observed_events
    assert res["ui_events"] == 2

    trace = trace_repo.get_trace(res["trace_id"])
    # 合并总数记录
    assert trace["extra"]["network_record_count"] == 2
    assert trace["extra"]["ui_event_count"] == 2
    # observed_events 自身计数
    assert trace["extra"]["observed_event_count"] == 2
    assert trace["extra"]["observed_event_merged_count"] == 2


def test_ingest_silent_failure_endpoint_persists_observed_fields():
    """服务端 /ingest/silent-failure 端点保留 observed + observed_events 并落库。

    注意：本用例只验证服务端字段透传与持久化，不验证 JS SDK 行为。
    SDK 端拼装逻辑需要手动跑 examples/silent_failure_demo.html 验证。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/ingest/silent-failure", json={
        "message": "点击提交按钮后无反应",
        "observed": "页面未跳转",
        "expectation": {"type": "route_change", "to": "/done"},
        "observed_events": [
            {
                "kind": "network",
                "data": {
                    "method": "POST",
                    "url": "http://x/api/submit",
                    "status_code": 200,
                    "duration_ms": 25,
                    "request_body": '{"name":"demo"}',
                },
            },
            {
                "kind": "ui",
                "data": {
                    "event_type": "click",
                    "target_selector": "#submit",
                    "route_path": "/page",
                },
            },
        ],
        "source": "browser_sdk",
        "trace_id": "sdk-trace-test-123",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert body["observed_event_count"] == 2
    assert body["observed_event_merged_count"] == 2
    assert body["observed_event_unknown_count"] == 0

    tid = body["trace_id"]
    trace = trace_repo.get_trace(tid)
    assert trace["extra"]["observed"] == "页面未跳转"
    assert trace["extra"]["observed_event_count"] == 2

    # 服务端按 kind 分类入库，AI 通过 get_debug_context 能拿到完整事件链
    assert len(trace_repo.get_network_records(tid)) == 1
    assert len(trace_repo.get_ui_events(tid)) == 1


# ── inbound 网络采集中间件（经 TestClient）──
def test_network_capture_middleware_records_inbound():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.middleware_network import NetworkCaptureMiddleware

    settings.network_capture_enabled = True
    try:
        app = FastAPI()

        @app.get("/ping")
        def ping():
            return {"ok": True}

        app.add_middleware(NetworkCaptureMiddleware)
        client = TestClient(app)

        resp = client.get("/ping")
        assert resp.status_code == 200
        rid = resp.headers.get("X-Debug-Request-Id")
        assert rid and rid.startswith("inbound-")

        records = trace_repo.get_network_records(rid)
        assert len(records) == 1
        assert records[0]["direction"] == "inbound"
        assert records[0]["method"] == "GET"
        assert records[0]["status_code"] == 200
    finally:
        settings.network_capture_enabled = False


def test_network_capture_middleware_disabled_by_default():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.middleware_network import NetworkCaptureMiddleware

    settings.network_capture_enabled = False
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    app.add_middleware(NetworkCaptureMiddleware)
    client = TestClient(app)

    resp = client.get("/ping")
    assert resp.status_code == 200
    assert "X-Debug-Request-Id" not in resp.headers  # 关闭时不记录

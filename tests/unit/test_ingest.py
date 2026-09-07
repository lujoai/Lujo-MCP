"""ingest_error 跨语言上报工具 + /ingest/error 路由单测"""
import pytest

from app.config import settings
from app.mcp.tools import ingest_api
from app.runtime.core import trace_repo


@pytest.fixture(autouse=True)
def _redaction_on():
    saved = settings.redaction_enabled
    settings.redaction_enabled = True
    yield
    settings.redaction_enabled = saved


def test_ingest_error_persists_trace():
    res = ingest_api.tool_ingest_error(
        exc_type="NullPointerException",
        message="cannot read property x of undefined",
        frames=[
            {"file": "AuthService.js", "line": 42, "function": "login", "code_context": "user.x"},
            {"no_file": True},  # 丢弃
        ],
        source="node_service",
        extra={"runtime": "node20"},
    )
    assert res["saved"] is True
    assert res["frame_count"] == 1  # 无效帧被丢弃

    trace = trace_repo.get_trace(res["trace_id"])
    assert trace["exc_type"] == "NullPointerException"
    assert trace["source"] == "node_service"
    assert trace["trace_kind"] == "exception"
    assert trace["extra"] == {"runtime": "node20"}
    assert trace["frames"][0]["file"] == "AuthService.js"


def test_ingest_error_redacts_message():
    res = ingest_api.tool_ingest_error(
        exc_type="Error",
        message='failed with password = "secret123"',
        frames=[],
    )
    trace = trace_repo.get_trace(res["trace_id"])
    assert "secret123" not in trace["message"]
    assert "***" in trace["message"]


def test_ingest_error_minimal():
    res = ingest_api.tool_ingest_error(exc_type="Err", message="boom")
    assert res["saved"] is True
    assert res["frame_count"] == 0
    trace = trace_repo.get_trace(res["trace_id"])
    assert trace["exc_type"] == "Err"


def test_ingest_error_route_via_testclient():
    """端到端：/ingest/error 路由可用且落库。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/ingest/error", json={
        "exc_type": "TypeError",
        "message": "x is undefined",
        "frames": [{"file": "a.js", "line": 10, "function": "f"}],
        "source": "test",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert body["frame_count"] == 1

    trace = trace_repo.get_trace(body["trace_id"])
    assert trace["exc_type"] == "TypeError"


def test_ingest_error_route_missing_fields_defaults():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # 只传 message，exc_type 缺省为 UnknownError
    resp = client.post("/ingest/error", json={"message": "something broke"})
    assert resp.status_code == 200
    assert resp.json()["saved"] is True


def test_ingest_error_route_hides_internal_exception(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.ingest as ingest_module

    app = FastAPI()
    app.include_router(ingest_module.router)
    client = TestClient(app)

    def _boom(**kwargs):
        raise RuntimeError("postgres://user:secret@localhost/db")

    monkeypatch.setattr(ingest_module, "tool_ingest_error", _boom)
    resp = client.post("/ingest/error", json={"message": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Internal server error"


def test_ingest_console_persists_record():
    from app.mcp.tools import console_api
    import uuid

    trace_id = "test-console-" + str(uuid.uuid4())[:8]
    res = console_api.tool_ingest_console(
        level="error",
        message="something went wrong",
        source="browser-sdk",
        extra={"session_id": "test-session"},
        trace_id=trace_id,
    )
    assert res["saved"] is True
    assert "console-" in res["record_id"]

    logs = trace_repo.get_console_logs(trace_id)
    assert len(logs) >= 1
    latest = logs[-1]
    assert latest["level"] == "error"
    assert latest["message"] == "something went wrong"
    assert latest["source"] == "browser-sdk"


def test_ingest_console_redacts_message():
    from app.mcp.tools import console_api
    import uuid

    trace_id = "test-console-redact-" + str(uuid.uuid4())[:8]
    console_api.tool_ingest_console(
        level="warn",
        message='api_key = "secret-token-123"',
        trace_id=trace_id,
    )
    logs = trace_repo.get_console_logs(trace_id)
    assert len(logs) >= 1
    latest = logs[-1]
    assert "secret-token-123" not in latest["message"]
    assert "***" in latest["message"]


def test_ingest_console_trace_id_association():
    from app.mcp.tools import console_api
    import uuid

    trace_id = "test-console-assoc-" + str(uuid.uuid4())[:8]
    res = console_api.tool_ingest_console(
        level="error",
        message="error with trace",
        trace_id=trace_id,
    )
    logs = trace_repo.get_console_logs(trace_id)
    assert len(logs) >= 1
    latest = logs[-1]
    assert latest["trace_id"] == trace_id
    assert latest["record_id"] == res["record_id"]


def test_ingest_console_route_via_testclient():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/ingest/console", json={
        "level": "error",
        "message": "test console error",
        "source": "browser-sdk",
        "extra": {"session_id": "test-session"},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert "console-" in body["record_id"]


def test_ingest_batch_rejects_over_limit():
    """P3-6: /ingest/batch events 超过 100 条返回 413"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    events = [{"path": "/ingest/error", "payload": {"message": f"e{i}"}} for i in range(101)]
    resp = client.post("/ingest/batch", json={"events": events})
    assert resp.status_code == 413
    assert resp.json()["detail"] == "Too many events in batch, max 100"


def test_ingest_batch_exact_limit_ok():
    """P3-6: /ingest/batch events 恰好 100 条应正常处理"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    events = [{"path": "/ingest/error", "payload": {"message": f"e{i}"}} for i in range(100)]
    resp = client.post("/ingest/batch", json={"events": events})
    assert resp.status_code == 200
    assert resp.json()["count"] == 100


# ── FIX: P1-A3 畸形 JSON 结构不产生 500 ──────────────────────────────


def _batch_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_ingest_batch_top_level_array_returns_422():
    """顶层 JSON 为数组（合法 JSON、非法结构）→ 422 而非 500。"""
    client = _batch_client()
    resp = client.post("/ingest/batch", json=[1, 2, 3])
    assert resp.status_code == 422


def test_ingest_batch_top_level_string_returns_422():
    """顶层 JSON 为字符串 → 422 而非 500。"""
    client = _batch_client()
    resp = client.post("/ingest/batch", json="abc")
    assert resp.status_code == 422


def test_ingest_batch_non_dict_event_returns_422():
    """events 元素非 dict（{"events":[1]}）→ 422 而非 500。"""
    client = _batch_client()
    resp = client.post("/ingest/batch", json={"events": [1]})
    assert resp.status_code == 422


def test_ingest_batch_mixed_bad_event_returns_422():
    """events 混入非 dict 元素 → 422。"""
    client = _batch_client()
    resp = client.post("/ingest/batch", json={
        "events": [{"path": "/ingest/error", "payload": {}}, "oops"]
    })
    assert resp.status_code == 422


def test_ingest_batch_empty_events_still_ok():
    """空 events 仍正常（非回归）。"""
    client = _batch_client()
    resp = client.post("/ingest/batch", json={"events": []})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0



# ---------------------------------------------------------------------------
# R7-A2：畸形 JSON / 非法 UTF-8 走 400（不再被 413 分支吞掉回显内部信息）
# ---------------------------------------------------------------------------


def _make_batch_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_batch_invalid_json_returns_400_not_413():
    """非法 JSON（JSONDecodeError 是 ValueError 子类）必须 400 + 固定文案。"""
    client = _make_batch_client()
    resp = client.post(
        "/ingest/batch",
        content=b"{not-valid-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid JSON body"


def test_batch_invalid_utf8_gzip_returns_400_not_413():
    """gzip 载荷含非法 UTF-8（UnicodeDecodeError 是 ValueError 子类）→ 400。"""
    import gzip as _gzip

    client = _make_batch_client()
    resp = client.post(
        "/ingest/batch",
        content=_gzip.compress(b"\xff\xfe not utf8"),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid JSON body"


def test_batch_gzip_bomb_still_413():
    """gzip 解压炸弹仍 413（专用异常，不再与 JSON 解析错误共用分支）。"""
    import gzip as _gzip
    import io as _io

    from app.api.ingest import _MAX_DECOMPRESSED_SIZE

    client = _make_batch_client()
    # 压缩后远小于 max_body_size(1MB)，解压后超过 10MB 上限
    bomb = _gzip.compress(b"\x00" * (_MAX_DECOMPRESSED_SIZE + 1))
    assert len(bomb) < 1024 * 1024
    resp = client.post(
        "/ingest/batch",
        content=bomb,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_batch_valid_gzip_payload_ok():
    """合法 gzip 压缩 batch 上报不受影响。"""
    import gzip as _gzip
    import json as _json

    client = _make_batch_client()
    payload = _json.dumps({
        "events": [{"path": "/ingest/error", "payload": {"message": "gzip-ok"}}]
    }).encode()
    resp = client.post(
        "/ingest/batch",
        content=_gzip.compress(payload),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# R2：session_id envelope（顶层规范位置 + 兼容旧 SDK 的 extra.session_id）
# ---------------------------------------------------------------------------


def test_ingest_error_session_from_extra_compat():
    """旧 SDK 把 session_id 放 extra：服务端必须兼容并参与会话分桶。

    FIX: R2 —— 此前服务端只读顶层 session_id，普通 SDK 数据进入 _global
    桶，会话查询无结果、相同错误跨页面被错误合并。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router
    from app.runtime.core import errors as errors_mod

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/ingest/error", json={
        "exc_type": "Error",
        "message": "boom",
        "frames": [{"file": "a.js", "line": 1, "function": "f"}],
        "extra": {"session_id": "sdk-sess-A"},
    })
    assert resp.status_code == 200
    tid = resp.json()["trace_id"]

    rec = errors_mod.get_by_id(tid, session_id="sdk-sess-A")
    assert rec is not None, "extra.session_id 应参与会话分桶"
    assert rec["session_id"] == "sdk-sess-A"
    assert errors_mod.get_by_id(tid, session_id="sdk-sess-B") is None


def test_ingest_error_session_top_level_wins():
    """顶层与 extra 同时携带 session_id 时顶层优先（规范位置）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router
    from app.runtime.core import errors as errors_mod

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/ingest/error", json={
        "exc_type": "Error",
        "message": "boom",
        "frames": [],
        "session_id": "top-level",
        "extra": {"session_id": "inner"},
    })
    tid = resp.json()["trace_id"]
    assert errors_mod.get_by_id(tid, session_id="top-level") is not None
    assert errors_mod.get_by_id(tid, session_id="inner") is None


def test_ingest_batch_session_from_extra_compat():
    """批量端点（SDK 实际上报路径）同样兼容 extra.session_id。"""
    client = _make_batch_client()
    resp = client.post("/ingest/batch", json={"events": [
        {
            "path": "/ingest/error",
            "payload": {
                "exc_type": "Error",
                "message": "batch boom",
                "frames": [],
                "extra": {"session_id": "batch-sess"},
            },
        }
    ]})
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["ok"] is True
    tid = result["result"]["trace_id"]

    from app.runtime.core import errors as errors_mod
    assert errors_mod.get_by_id(tid, session_id="batch-sess") is not None


def test_two_sessions_same_code_not_merged():
    """两个会话在同一源码位置报相同错误：各自可查询且互不合并；
    同一会话内重复错误仍正确聚合。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router
    from app.runtime.core import errors as errors_mod

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    payload = {
        "exc_type": "TypeError",
        "message": "Cannot read properties of undefined",
        "frames": [{"file": "login.js", "line": 42, "function": "submit"}],
    }
    r1 = client.post("/ingest/error", json={**payload, "extra": {"session_id": "sess-1"}}).json()
    r2 = client.post("/ingest/error", json={**payload, "extra": {"session_id": "sess-2"}}).json()
    # 跨会话：不同 error_id（不合并）
    assert r1["trace_id"] != r2["trace_id"]
    # 各自可查
    assert errors_mod.get_by_id(r1["trace_id"], session_id="sess-1") is not None
    assert errors_mod.get_by_id(r2["trace_id"], session_id="sess-2") is not None
    # 同会话重复：聚合到同一条
    r3 = client.post("/ingest/error", json={**payload, "extra": {"session_id": "sess-1"}}).json()
    assert r3["trace_id"] == r1["trace_id"]
    assert errors_mod.get_by_id(r1["trace_id"], session_id="sess-1")["occurrence_count"] == 2


def test_event_routes_propagate_session_id_and_filter_context():
    """R2/R5：network、UI、console 单条端点都保存并隔离 session_id。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    trace_id = "event-session-route"

    responses = [
        client.post("/ingest/network", json={
            "record": {"url": "http://route/network", "status_code": 500},
            "trace_id": trace_id,
            "session_id": "session-a",
        }),
        client.post("/ingest/ui-event", json={
            "event": {"event_type": "click", "target_selector": "#a"},
            "trace_id": trace_id,
            "session_id": "session-a",
        }),
        client.post("/ingest/console", json={
            "level": "error",
            "message": "route console",
            "trace_id": trace_id,
            "session_id": "session-a",
        }),
    ]
    assert all(response.status_code == 200 for response in responses)

    assert trace_repo.get_network_records(trace_id, session_id="session-a")
    assert trace_repo.get_ui_events(trace_id, session_id="session-a")
    assert trace_repo.get_console_logs(trace_id, session_id="session-a")
    assert trace_repo.get_network_records(trace_id, session_id="session-b") == []
    assert trace_repo.get_ui_events(trace_id, session_id="session-b") == []
    assert trace_repo.get_console_logs(trace_id, session_id="session-b") == []


def test_batch_event_routes_propagate_session_id():
    """R2：batch 的三类现场事件使用同一 envelope 会话规则。"""
    client = _make_batch_client()
    trace_id = "event-session-batch"
    resp = client.post("/ingest/batch", json={"events": [
        {"path": "/ingest/network", "payload": {
            "record": {"url": "http://batch/network"},
            "trace_id": trace_id,
            "session_id": "batch-session",
        }},
        {"path": "/ingest/ui-event", "payload": {
            "event": {"event_type": "click"},
            "trace_id": trace_id,
            "session_id": "batch-session",
        }},
        {"path": "/ingest/console", "payload": {
            "message": "batch console",
            "trace_id": trace_id,
            "session_id": "batch-session",
        }},
    ]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert all(item["ok"] for item in body["results"])
    assert trace_repo.get_network_records(trace_id, session_id="batch-session")
    assert trace_repo.get_ui_events(trace_id, session_id="batch-session")
    assert trace_repo.get_console_logs(trace_id, session_id="batch-session")
    assert trace_repo.get_network_records(trace_id, session_id="other-session") == []


# ---------------------------------------------------------------------------
# R4：入库规范化保留 column（Source Map 精确还原必需）
# ---------------------------------------------------------------------------


def test_parse_frames_preserves_column():
    """column 保留；缺失/非数值时不伪造 0（按缺失处理）。"""
    frames = ingest_api._parse_frames([
        {"file": "app.js", "line": 1, "function": "login", "column": 51},
        {"file": "app.js", "line": 2, "function": "no-column"},
        {"file": "app.js", "line": 3, "function": "bad", "column": "abc"},
        {"file": "app.js", "line": 4, "function": "fraction", "column": 1.5},
        {"file": "app.js", "line": 5, "function": "bool", "column": True},
    ])
    assert frames[0]["column"] == 51
    assert "column" not in frames[1]
    assert "column" not in frames[2]
    assert "column" not in frames[3]
    assert "column" not in frames[4]
    # 原有字段不回归
    assert frames[0]["file"] == "app.js"
    assert frames[0]["function"] == "login"


def test_ingest_error_preserves_column_end_to_end():
    """真实 SDK 形状帧经 tool_ingest_error 落库后 column 仍在。"""
    res = ingest_api.tool_ingest_error(
        exc_type="Error",
        message="boom",
        frames=[{"file": "http://x/app.js", "line": 1, "function": "f", "column": 51}],
    )
    trace = trace_repo.get_trace(res["trace_id"])
    assert trace["frames"][0].get("column") == 51


def test_silent_failure_parse_frames_preserves_column():
    """silent-failure 帧转换入口同一契约（R4 审查的其他转换入口）。"""
    from app.mcp.tools import silent_failure_api

    frames = silent_failure_api._parse_frames([
        {"file": "app.js", "line": 1, "function": "f", "column": 12},
        {"file": "app.js", "line": 2, "function": "g"},
    ])
    assert frames[0]["column"] == 12
    assert "column" not in frames[1]

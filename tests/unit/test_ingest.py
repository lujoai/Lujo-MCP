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

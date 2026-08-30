"""network 采集器 + ingest_network/get_network_trace 工具单测"""
import pytest

from app.config import settings
from app.runtime.collectors import network as net_collector
from app.mcp.tools import network_api
from app.runtime.core import trace_repo


@pytest.fixture(autouse=True)
def _redaction_on():
    saved = settings.redaction_enabled
    settings.redaction_enabled = True
    yield
    settings.redaction_enabled = saved


def test_parse_normalizes_fields():
    rec = net_collector.parse_network_record({
        "method": "post", "status_code": "200", "duration_ms": "12.5",
        "url": "http://x", "request_body": "b",
    })
    assert rec["method"] == "POST"
    assert rec["status_code"] == 200
    assert rec["duration_ms"] == 12.5
    assert rec["direction"] == "outbound"
    assert rec["record_id"] is None  # 由存储层生成


def test_parse_truncates_long_body():
    long_body = "x" * (net_collector._MAX_BODY_CHARS + 1000)
    rec = net_collector.parse_network_record({"response_body": long_body})
    assert len(rec["response_body"]) < len(long_body)
    assert "已截断" in rec["response_body"]


# ---------------------------------------------------------------------------
# FIX(v0.7.1-b3-7): url 截断 + timestamp 类型归一
# ---------------------------------------------------------------------------


def test_parse_truncates_long_url():
    long_url = "http://example.com/" + "a" * (net_collector._MAX_URL_CHARS + 500)
    rec = net_collector.parse_network_record({"url": long_url})
    assert len(rec["url"]) == net_collector._MAX_URL_CHARS + len("...（已截断）")
    assert "已截断" in rec["url"]


def test_parse_timestamp_normalized_to_numeric():
    """字符串/None/bool 时间戳此前原样透传，现归一为数值（回退当前时间）。"""
    import time as _time

    rec = net_collector.parse_network_record({"timestamp": 123456.0})
    assert rec["timestamp"] == 123456.0  # 合法数值原样保留
    assert isinstance(rec["timestamp"], float)

    for bad in ("now", None, True):
        parsed = net_collector.parse_network_record({"timestamp": bad})
        assert isinstance(parsed["timestamp"], float), (
            f"非数值时间戳 {bad!r} 必须归一为数值，实际 {type(parsed['timestamp'])}"
        )
        # 回退当前时间（与调用时刻近似）
        assert 0 < parsed["timestamp"] <= _time.time() + 5


def test_parse_rejects_non_dict():
    with pytest.raises(ValueError):
        net_collector.parse_network_record("not a dict")  # type: ignore


def test_parse_batch_skips_invalid():
    out = net_collector.parse_network_records([
        {"method": "GET", "url": "http://a"},
        "invalid",  # 跳过
        {"method": "POST", "url": "http://b"},
    ])
    assert len(out) == 2
    assert {r["url"] for r in out} == {"http://a", "http://b"}


def test_tool_ingest_and_get_roundtrip():
    tid = trace_repo.save_trace("E", "m", [])
    res = network_api.tool_ingest_network(
        {"method": "get", "url": "http://x/api", "status_code": 200, "duration_ms": 5.0},
        trace_id=tid,
    )
    assert res["saved"] is True
    assert res["record_id"]

    got = network_api.tool_get_network_trace(tid)
    assert got["found"] is True
    assert got["count"] == 1
    assert got["records"][0]["method"] == "GET"
    assert got["records"][0]["trace_id"] == tid


def test_tool_ingest_redacts_body_at_storage_boundary():
    tid = trace_repo.save_trace("E", "m", [])
    network_api.tool_ingest_network(
        {"url": "http://x/?token=secret", "request_body": 'password = "pw"'},
        trace_id=tid,
    )
    rec = network_api.tool_get_network_trace(tid)["records"][0]
    assert "secret" not in rec["url"]
    assert "pw" not in rec["request_body"]


def test_get_network_trace_empty_for_unknown():
    got = network_api.tool_get_network_trace("no-such-trace")
    assert got["found"] is False
    assert got["count"] == 0


def test_tool_ingest_redacts_json_request_body():
    tid = trace_repo.save_trace("E", "m", [])
    network_api.tool_ingest_network(
        {"url": "http://x/api/login", "request_body": '{"password":"123456","username":"admin"}'},
        trace_id=tid,
    )
    rec = network_api.tool_get_network_trace(tid)["records"][0]
    assert "123456" not in rec["request_body"]
    assert "admin" in rec["request_body"]


def test_tool_ingest_redacts_json_response_body():
    tid = trace_repo.save_trace("E", "m", [])
    network_api.tool_ingest_network(
        {"url": "http://x/api/token", "response_body": '{"token":"sk-xxx-123","user_id":123}'},
        trace_id=tid,
    )
    rec = network_api.tool_get_network_trace(tid)["records"][0]
    assert "sk-xxx-123" not in rec["response_body"]
    assert "123" in rec["response_body"]


def test_ingest_network_route_via_testclient():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ingest import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/ingest/network", json={
        "record": {
            "url": "http://example.com/api/test",
            "method": "GET",
            "status_code": 200,
            "duration_ms": 100,
            "request_body": '{"password":"secret"}',
            "response_body": '{"token":"abc123"}',
        },
        "source": "browser-sdk",
        "extra": {"session_id": "test-session"},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert body["record_id"]

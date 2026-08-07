"""ui_event 采集器单测"""
import pytest

from app.config import settings
from app.runtime.collectors import ui_event as ui_collector
from app.runtime.core import trace_repo


@pytest.fixture(autouse=True)
def _redaction_on():
    saved = settings.redaction_enabled
    settings.redaction_enabled = True
    yield
    settings.redaction_enabled = saved


def test_parse_normalizes_fields():
    ev = ui_collector.parse_ui_event({"event_type": "Click", "target_selector": "#btn"})
    assert ev["event_type"] == "Click"  # 保留原始大小写（事件类型语义）
    assert ev["target_selector"] == "#btn"
    assert ev["event_id"] is None  # 由存储层生成
    assert ev["timestamp"]  # 自动补时间戳


def test_parse_defaults_event_type():
    ev = ui_collector.parse_ui_event({"target_selector": "#x"})
    assert ev["event_type"] == "click"


def test_parse_truncates_long_payload():
    long_payload = "x" * (ui_collector._MAX_PAYLOAD_CHARS + 500)
    ev = ui_collector.parse_ui_event({"payload_json": long_payload})
    assert len(ev["payload_json"]) < len(long_payload)
    assert "已截断" in ev["payload_json"]


def test_parse_rejects_non_dict():
    with pytest.raises(ValueError):
        ui_collector.parse_ui_event("not a dict")  # type: ignore


def test_parse_batch_skips_invalid():
    out = ui_collector.parse_ui_events([
        {"event_type": "click", "target_selector": "#a"},
        "invalid",  # 跳过
        {"event_type": "submit", "target_selector": "#b"},
    ])
    assert len(out) == 2
    assert {e["target_selector"] for e in out} == {"#a", "#b"}


def test_roundtrip_with_trace_repo_and_redaction():
    tid = trace_repo.save_trace("E", "m", [])
    eid = trace_repo.save_ui_event(
        ui_collector.parse_ui_event({
            "event_type": "submit",
            "route_path": "/page?token=secret",
            "payload_json": 'password = "pw"',
        }),
        trace_id=tid,
    )
    events = trace_repo.get_ui_events(tid)
    assert len(events) == 1
    assert events[0]["event_id"] == eid
    assert events[0]["event_type"] == "submit"
    # 存储边界脱敏
    assert "secret" not in events[0]["route_path"]
    assert "pw" not in events[0]["payload_json"]

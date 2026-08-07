"""指纹去重 + 聚合单测（M10）"""
import pytest

from app.runtime.core import errors
from app.mcp.core import trace_repo
from app.mcp.tools import trace_api


@pytest.fixture(autouse=True)
def _clear():
    errors._recent.clear()
    yield
    errors._recent.clear()


def _frames():
    return [{"file": "a.py", "line": 10, "function": "f"}]


def test_same_fingerprint_aggregates_occurrence():
    id1 = errors.record({"type": "ValueError", "message": "bad", "frames": _frames()}, source="t")
    id2 = errors.record({"type": "ValueError", "message": "bad again", "frames": _frames()}, source="t")
    assert id1 == id2  # 同指纹 → 同 error_id，聚合

    entry = errors.get_by_id(id1)
    assert entry["occurrence_count"] == 2
    assert entry["last_seen"] >= entry["first_seen"]


def test_different_fingerprint_separate():
    id1 = errors.record({"type": "ValueError", "message": "x", "frames": _frames()}, source="t")
    id2 = errors.record({"type": "TypeError", "message": "y", "frames": _frames()}, source="t")
    assert id1 != id2  # 不同异常类型 → 不同指纹
    assert errors.get_by_id(id1)["occurrence_count"] == 1
    assert errors.get_by_id(id2)["occurrence_count"] == 1


def test_line_number_difference_still_deduped():
    """指纹忽略行号差异：同 file:function 不同行 → 仍聚合。"""
    id1 = errors.record({"type": "E", "message": "m",
                         "frames": [{"file": "a.py", "line": 10, "function": "f"}]}, source="t")
    id2 = errors.record({"type": "E", "message": "m",
                         "frames": [{"file": "a.py", "line": 99, "function": "f"}]}, source="t")
    assert id1 == id2
    assert errors.get_by_id(id1)["occurrence_count"] == 2


def test_list_recent_sorted_by_last_seen_desc():
    import time
    id1 = errors.record({"type": "E1", "message": "m", "frames": []}, source="t")
    time.sleep(0.01)
    id2 = errors.record({"type": "E2", "message": "m", "frames": []}, source="t")
    items = errors.list_recent(10)
    assert items[0]["error_id"] == id2  # 最新的在前
    assert items[1]["error_id"] == id1


def test_trace_repo_get_trace_exposes_aggregation():
    tid = trace_repo.save_trace("ValueError", "m", _frames(), source="t")
    # 再存一次同指纹
    trace_repo.save_trace("ValueError", "m2", _frames(), source="t")
    got = trace_repo.get_trace(tid)
    assert got["occurrence_count"] == 2
    assert got["fingerprint"]
    assert got["last_seen"] >= got["first_seen"]


def test_trace_api_summary_includes_fingerprint_and_count():
    trace_repo.save_trace("ValueError", "m", _frames(), source="t")
    trace_repo.save_trace("ValueError", "m2", _frames(), source="t")
    summaries = trace_api.list_recent_traces(10)
    assert summaries
    s = summaries[0]
    assert s["occurrence_count"] == 2
    assert s["fingerprint"]
    assert s["top_frame"] == "a.py:10 in f"
    assert "trace_id" in s and "last_seen" in s and "first_seen" in s


def test_search_logs_includes_aggregation():
    trace_repo.save_trace("ValueError", "unique-kw-msg", _frames(), source="t")
    trace_repo.save_trace("ValueError", "unique-kw-msg", _frames(), source="t")
    res = trace_api.search_logs("unique-kw", since_minutes=60)
    assert len(res) == 1  # 去重后只一条
    assert res[0]["occurrence_count"] == 2

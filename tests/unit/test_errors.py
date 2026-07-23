"""errors 模块会话隔离单元测试"""
import time


from app.mcp.core import errors


def _frames():
    return [{"file": "a.py", "line": 10, "function": "f"}]


def _frames_b():
    return [{"file": "b.py", "line": 20, "function": "g"}]


# 1. record 按 session_id 分桶
def test_record_with_session_id():
    errors.record({"type": "ValueError", "message": "va", "frames": _frames()}, source="t", session_id="sess-a")
    assert "sess-a" in errors._recent
    assert len(errors._recent["sess-a"]) == 1

    errors.record({"type": "TypeError", "message": "tb", "frames": _frames_b()}, source="t", session_id="sess-b")
    assert "sess-b" in errors._recent
    assert len(errors._recent["sess-b"]) == 1
    # sess-a 不受影响
    assert len(errors._recent["sess-a"]) == 1
    assert errors._recent["sess-a"][0]["type"] == "ValueError"


# 2. session_id=None 写入 _global 桶
def test_record_without_session_id():
    errors.record({"type": "RuntimeError", "message": "g", "frames": _frames()}, source="t")
    assert "_global" in errors._recent
    assert len(errors._recent["_global"]) == 1
    assert errors._recent["_global"][0]["type"] == "RuntimeError"


# 3. list_recent 按 session 过滤
def test_list_recent_filter_by_session():
    errors.record({"type": "ValueError", "message": "a1", "frames": _frames()}, source="t", session_id="sess-a")
    errors.record({"type": "TypeError", "message": "b1", "frames": _frames_b()}, source="t", session_id="sess-b")

    a_items = errors.list_recent(session_id="sess-a")
    assert len(a_items) == 1
    assert a_items[0]["type"] == "ValueError"

    b_items = errors.list_recent(session_id="sess-b")
    assert len(b_items) == 1
    assert b_items[0]["type"] == "TypeError"

    # None → 聚合所有桶
    all_items = errors.list_recent()
    assert len(all_items) == 2


# 4. search 按 session 过滤
def test_search_filter_by_session():
    errors.record({"type": "ValueError", "message": "keyword-alpha", "frames": _frames()}, source="t", session_id="sess-a")
    errors.record({"type": "ValueError", "message": "keyword-beta", "frames": _frames()}, source="t", session_id="sess-b")

    a_results = errors.search("keyword", since_minutes=60, session_id="sess-a")
    assert len(a_results) == 1
    assert a_results[0]["message"] == "keyword-alpha"

    all_results = errors.search("keyword", since_minutes=60)
    assert len(all_results) == 2


# 5. get_by_id 按 session 过滤
def test_get_by_id_filter_by_session():
    error_id = errors.record({"type": "KeyError", "message": "k", "frames": _frames()}, source="t", session_id="sess-a")

    # 同 session 能命中
    found = errors.get_by_id(error_id, session_id="sess-a")
    assert found is not None
    assert found["error_id"] == error_id

    # 其他 session 找不到
    assert errors.get_by_id(error_id, session_id="sess-b") is None

    # None → 全局查找
    found_global = errors.get_by_id(error_id)
    assert found_global is not None
    assert found_global["error_id"] == error_id


# 6. 指纹去重仅在桶内生效
def test_fingerprint_dedup_within_bucket_only():
    errors.record({"type": "ValueError", "message": "same", "frames": _frames()}, source="t", session_id="sess-a")
    errors.record({"type": "ValueError", "message": "same", "frames": _frames()}, source="t", session_id="sess-b")

    a_entry = errors._recent["sess-a"][0]
    b_entry = errors._recent["sess-b"][0]
    assert a_entry["occurrence_count"] == 1
    assert b_entry["occurrence_count"] == 1
    # 不同桶，不同 error_id
    assert a_entry["error_id"] != b_entry["error_id"]

    # 再次向 sess-a 写入同指纹
    errors.record({"type": "ValueError", "message": "same again", "frames": _frames()}, source="t", session_id="sess-a")
    assert errors._recent["sess-a"][0]["occurrence_count"] == 2
    # sess-b 不受影响
    assert errors._recent["sess-b"][0]["occurrence_count"] == 1


# 7. get_latest 按 session 过滤
def test_get_latest_filter_by_session():
    errors.record({"type": "ErrorA", "message": "a", "frames": _frames()}, source="t", session_id="sess-a")
    time.sleep(0.01)
    errors.record({"type": "ErrorB", "message": "b", "frames": _frames_b()}, source="t", session_id="sess-b")

    latest_a = errors.get_latest(session_id="sess-a")
    assert latest_a is not None
    assert latest_a["type"] == "ErrorA"

    latest_b = errors.get_latest(session_id="sess-b")
    assert latest_b is not None
    assert latest_b["type"] == "ErrorB"

    # 全局 → last_seen 最大的
    latest_all = errors.get_latest()
    assert latest_all is not None
    assert latest_all["type"] == "ErrorB"

"""errors 模块会话隔离单元测试"""
import asyncio
import time
import pytest

from app.runtime.core import errors


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


# 8. R3-7: bucket 总数 LRU 上限（防高频伪造 session 无界撑爆内存）
def test_bucket_total_lru_cap(monkeypatch):
    from collections import OrderedDict

    monkeypatch.setattr(errors, "_MAX_BUCKETS", 5)
    saved_recent = errors._recent
    errors._recent = OrderedDict()  # 隔离既有桶，保证断言确定性
    try:
        for i in range(8):
            errors.record(
                {"type": "ValueError", "message": f"m{i}",
                 "frames": [{"file": f"f{i}.py", "line": 1, "function": "fn"}]},
                source="t", session_id=f"cap-{i}",
            )
        # 总数被限制在上限内，最旧的 bucket 已淘汰
        assert len(errors._recent) == 5
        assert "cap-0" not in errors._recent
        assert "cap-7" in errors._recent

        # LRU 语义：写入已存在 bucket 会刷新其位置，不被下一次淘汰
        errors.record(
            {"type": "ValueError", "message": "refresh",
             "frames": [{"file": "r.py", "line": 1, "function": "fn"}]},
            source="t", session_id="cap-5",
        )
        errors.record(
            {"type": "ValueError", "message": "new",
             "frames": [{"file": "n.py", "line": 1, "function": "fn"}]},
            source="t", session_id="cap-8",
        )
        assert len(errors._recent) == 5
        assert "cap-5" in errors._recent      # 刚刷新，保留
        assert "cap-3" not in errors._recent  # 队首最旧，被淘汰
    finally:
        errors._recent = saved_recent


# 9. P1-9e: _schedule_pg_upsert 按 (fingerprint, session_id) 节流
def test_pg_upsert_throttle_key_session_isolation(monkeypatch):
    """验证 _schedule_pg_upsert 采用 (fingerprint, session_id) 双键节流：
    - 同一 session 的同指纹错误在 2s 窗口内被节流跳过
    - 不同 session 的同指纹错误相互隔离，不被跨会话静默丢弃
    """
    from app.config import settings
    monkeypatch.setattr(settings, "storage_backend", "postgresql")
    monkeypatch.setattr(settings, "pg_async_enabled", False)

    scheduled = []

    def fake_pg_upsert(record_data):
        scheduled.append(record_data)

    monkeypatch.setattr(errors, "_pg_upsert_error", fake_pg_upsert)
    errors._last_scheduled.clear()

    rec_a1 = {"fingerprint": "fp-shared", "session_id": "sess-alpha", "type": "ValueError"}
    rec_a2 = {"fingerprint": "fp-shared", "session_id": "sess-alpha", "type": "ValueError"}
    rec_b1 = {"fingerprint": "fp-shared", "session_id": "sess-beta", "type": "ValueError"}

    errors._schedule_pg_upsert(rec_a1)
    errors._schedule_pg_upsert(rec_a2)  # 同 session 节流
    errors._schedule_pg_upsert(rec_b1)  # 不同 session 允许落库

    time.sleep(0.1)
    sessions = [r["session_id"] for r in scheduled]
    assert sessions.count("sess-alpha") == 1
    assert sessions.count("sess-beta") == 1


# 10. Phase 3.1: pg_async_enabled=True 时调度 AsyncPGErrorStore
@pytest.mark.asyncio
async def test_pg_async_enabled_upsert_scheduling(monkeypatch):
    """验证 pg_async_enabled=True 时 _schedule_pg_upsert 调度 AsyncPGErrorStore.upsert_error。"""
    from app.config import settings
    from app.runtime.core.storage.async_pg_store import AsyncPGErrorStore

    monkeypatch.setattr(settings, "storage_backend", "postgresql")
    monkeypatch.setattr(settings, "pg_async_enabled", True)

    scheduled = []

    async def fake_async_upsert(self, record_data):
        scheduled.append(record_data)

    monkeypatch.setattr(AsyncPGErrorStore, "upsert_error", fake_async_upsert)
    errors._last_scheduled.clear()

    rec = {"fingerprint": "fp-async-1", "session_id": "sess-async", "type": "RuntimeError"}
    errors._schedule_pg_upsert(rec)

    await asyncio.sleep(0.05)
    assert len(scheduled) == 1
    assert scheduled[0]["fingerprint"] == "fp-async-1"



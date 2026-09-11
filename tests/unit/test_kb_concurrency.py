"""B04 并发持久化倒序与竞争测试。

覆盖：
1. 并发 upsert 倒序：旧写晚于新写落库，不得覆盖持久层新分析；
2. 并发 record_verification 倒序：旧统计晚于新统计落库，不得覆盖持久层；
3. clear 与 upsert 竞争：clear 期间/之后的旧写不得复活到持久层。
"""
import threading
import time

import pytest

import app.rag.knowledge_base as kb_module
from app.rag.knowledge_base import KnowledgeBaseStore
from tests.unit.test_kb_persistence import FakeKnowledgeBaseStore


def _make_store_with_fake():
    fake = FakeKnowledgeBaseStore()
    store = KnowledgeBaseStore(max_entries=10)
    return store, fake


def test_concurrent_upsert_out_of_order_preserves_latest(monkeypatch):
    """B04: 同指纹并发 upsert 锁外持久化倒序时，不得用旧快照覆盖新快照。"""
    store, fake = _make_store_with_fake()
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: fake)

    t1_started = threading.Event()
    t2_finished = threading.Event()

    orig_upsert_kb = fake.upsert_kb_entry

    def delayed_upsert(entry):
        if entry["analysis"].get("root_cause") == "old_cause":
            t1_started.set()
            # 等待线程 2 完成写入
            assert t2_finished.wait(timeout=2.0)
            # 延迟一下，确保晚于线程 2 写入
            time.sleep(0.05)
        orig_upsert_kb(entry)

    fake.upsert_kb_entry = delayed_upsert

    def worker_old():
        store.upsert(
            fingerprint="fp-concur",
            analysis={"exception_type": "ValueError", "root_cause": "old_cause"},
            fix_suggestion="old_fix",
            source="llm",
        )

    def worker_new():
        t1_started.wait(timeout=2.0)
        store.upsert(
            fingerprint="fp-concur",
            analysis={"exception_type": "ValueError", "root_cause": "new_cause"},
            fix_suggestion="new_fix",
            source="llm",
        )
        t2_finished.set()

    th1 = threading.Thread(target=worker_old)
    th2 = threading.Thread(target=worker_new)

    th1.start()
    th2.start()

    th1.join(timeout=3.0)
    th2.join(timeout=3.0)

    assert not th1.is_alive()
    assert not th2.is_alive()

    # 持久层最终状态必须是最新快照 "new_cause"，不能被旧写覆盖为 "old_cause"
    persisted = fake.rows.get("fp-concur")
    assert persisted is not None
    assert persisted["analysis"]["root_cause"] == "new_cause"


def test_concurrent_verification_out_of_order_preserves_latest(monkeypatch):
    """B04: 同指纹并发验证回写倒序时，不得用旧统计覆盖新统计。"""
    store, fake = _make_store_with_fake()
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: fake)

    store.upsert(
        fingerprint="fp-verify",
        analysis={"exception_type": "ValueError"},
        fix_suggestion="fix",
        source="llm",
    )

    t1_started = threading.Event()
    t2_finished = threading.Event()

    orig_update_kb = fake.update_kb_verification

    def delayed_update(fp, vc, conf, up_at):
        if vc == 1:
            t1_started.set()
            assert t2_finished.wait(timeout=2.0)
            time.sleep(0.05)
        return orig_update_kb(fp, vc, conf, up_at)

    fake.update_kb_verification = delayed_update

    def worker_v1():
        store.record_verification("fp-verify", 0.6)

    def worker_v2():
        t1_started.wait(timeout=2.0)
        store.record_verification("fp-verify", 0.9)
        t2_finished.set()

    th1 = threading.Thread(target=worker_v1)
    th2 = threading.Thread(target=worker_v2)

    th1.start()
    th2.start()

    th1.join(timeout=3.0)
    th2.join(timeout=3.0)

    persisted = fake.rows.get("fp-verify")
    assert persisted is not None
    assert persisted["verify_count"] == 2
    assert persisted["case_confidence"] == 0.9


def test_clear_concurrent_with_in_flight_upsert(monkeypatch):
    """B04: clear 执行期间/之后，旧并发写入不得复活落库。"""
    store, fake = _make_store_with_fake()
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: fake)

    upsert_started = threading.Event()
    clear_finished = threading.Event()

    orig_upsert_kb = fake.upsert_kb_entry

    def delayed_upsert(entry):
        upsert_started.set()
        assert clear_finished.wait(timeout=2.0)
        time.sleep(0.05)
        orig_upsert_kb(entry)

    fake.upsert_kb_entry = delayed_upsert

    def worker_upsert():
        store.upsert(
            fingerprint="fp-ghost",
            analysis={"exception_type": "ValueError"},
            fix_suggestion="fix",
            source="llm",
        )

    def worker_clear():
        upsert_started.wait(timeout=2.0)
        assert store.clear() is True
        clear_finished.set()

    th_upsert = threading.Thread(target=worker_upsert)
    th_clear = threading.Thread(target=worker_clear)

    th_upsert.start()
    th_clear.start()

    th_upsert.join(timeout=3.0)
    th_clear.join(timeout=3.0)

    # clear 完成后持久层不应有幽灵数据复活
    assert "fp-ghost" not in fake.rows
    assert len(fake.rows) == 0

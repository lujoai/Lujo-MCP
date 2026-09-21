"""B04 并发持久化倒序与竞争测试。

覆盖：
1. 并发 upsert 倒序：旧写晚于新写落库，不得覆盖持久层新分析；
2. 并发 record_verification 倒序：旧统计晚于新统计落库，不得覆盖持久层；
3. clear 与 upsert 竞争：clear 期间/之后的旧写不得复活到持久层。
"""
import threading
import time

from app.rag.knowledge_base import KnowledgeBaseStore
from tests.unit.test_kb_persistence import FakeKnowledgeBaseStore


def _make_store_with_fake():
    """AD-1 方案 B：经 persist_store 显式注入持久化替身。"""
    fake = FakeKnowledgeBaseStore()
    store = KnowledgeBaseStore(persist_store=fake, max_entries=10)
    return store, fake


def test_concurrent_upsert_out_of_order_preserves_latest():
    """B04: 同指纹并发 upsert 锁外持久化倒序时，不得用旧快照覆盖新快照。"""
    store, fake = _make_store_with_fake()

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


def test_concurrent_verification_out_of_order_preserves_latest():
    """B04: 同指纹并发验证回写倒序时，不得用旧统计覆盖新统计。"""
    store, fake = _make_store_with_fake()

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


def test_clear_concurrent_with_in_flight_upsert():
    """B04: clear 执行期间/之后，旧并发写入不得复活落库。"""
    store, fake = _make_store_with_fake()

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


def test_clear_and_concurrent_upsert_never_diverge():
    """W13 / P2-STORE-1：clear 与并发 upsert 交错时，内存与持久层不得分叉。

    旧时序是「锁内 gen-bump → **锁外** delete-all → 锁内清内存」，于是存在这个
    交错：upsert 在 gen-bump 之后拿到锁写入内存（它的 clear 快照 == 新的
    ``_clear_generation``，代际守卫因此不认为自己被超越），而它的落库又发生在
    delete-all **之后** → 持久层留下条目、内存已被清空 → 重启回灌把用户刚清掉的
    条目复活。反方向（落库在 delete-all 之前、内存写入在清内存之后）产生对称分叉。

    本用例用事件把该交错钉成确定性顺序，断言的是**一致性不变量**
    （内存有 ⟺ 持久层有），而不是某一方必胜——修复后两种结局都合法。
    """
    store, fake = _make_store_with_fake()

    # 先落一条已持久化的条目，让 delete-all 真的有事可做（用原始替身，不编排）
    store.upsert(
        fingerprint="fp-old",
        analysis={"exception_type": "ValueError", "message": "old"},
        fix_suggestion="old-fix",
        source="llm",
    )
    assert "fp-old" in fake.rows

    delete_all_entered = threading.Event()
    delete_all_release = threading.Event()
    clear_returned = threading.Event()
    persist_reached = threading.Event()

    orig_delete_all = fake.delete_all_kb_entries
    orig_upsert_kb = fake.upsert_kb_entry

    def blocked_delete_all():
        delete_all_entered.set()
        assert delete_all_release.wait(timeout=5.0), "编排超时：delete-all 未被放行"
        return orig_delete_all()

    def deferred_upsert(entry):
        # 落库刻意推迟到 clear() 返回之后 —— 这正是旧时序产生「持久层有、内存无」的窗口
        persist_reached.set()
        assert clear_returned.wait(timeout=5.0), "编排超时：clear 未返回"
        orig_upsert_kb(entry)

    fake.delete_all_kb_entries = blocked_delete_all
    fake.upsert_kb_entry = deferred_upsert

    clear_result: list = []

    def worker_clear():
        clear_result.append(store.clear())
        clear_returned.set()

    def worker_upsert():
        store.upsert(
            fingerprint="fp-new",
            analysis={"exception_type": "ValueError", "message": "new"},
            fix_suggestion="new-fix",
            source="llm",
        )

    th_clear = threading.Thread(target=worker_clear)
    th_clear.start()
    assert delete_all_entered.wait(timeout=5.0), "clear 未进入 delete-all"

    th_upsert = threading.Thread(target=worker_upsert)
    th_upsert.start()

    # 有界等待，仅用于决定编排顺序（不是对时序的断言）：
    # 旧时序下 upsert 能在 clear 的锁外窗口里写完内存并抵达落库点；
    # 修复后它会阻塞在 clear 持有的锁上，永远到不了落库点。
    reached_persist_before_clear_done = persist_reached.wait(timeout=1.0)

    delete_all_release.set()
    th_clear.join(timeout=5.0)
    th_upsert.join(timeout=5.0)
    assert not th_clear.is_alive() and not th_upsert.is_alive(), "线程未收尾"
    assert clear_result == [True], "持久层删除成功时 clear 必须返回 True"

    in_memory = store.get("fp-new") is not None
    in_persist = "fp-new" in fake.rows
    assert in_memory == in_persist, (
        "内存与持久层分叉（P2-STORE-1）：in_memory=%s in_persist=%s "
        "（upsert 是否在 clear 完成前抵达落库点=%s）"
        % (in_memory, in_persist, reached_persist_before_clear_done)
    )
    assert "fp-old" not in fake.rows, "clear 之前落库的条目必须被删掉"

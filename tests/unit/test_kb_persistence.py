"""v0.5.3 KB 持久化（写穿 + 启动回灌）单元测试。

用内存版 FakeKnowledgeBaseStore 替换 factory 分发的持久化实例，
验证 KnowledgeBaseStore 的写穿行为与回灌行为；PG 真实落库由
tests/integration/test_pg_integration.py 覆盖。
"""

import time

import pytest

import app.rag.knowledge_base as kb_module
from app.rag.knowledge_base import KnowledgeBaseStore
from app.runtime.core.storage.base import KnowledgeBaseStorage
from app.runtime.core.storage.noop_store import NoOpKnowledgeBaseStore


class FakeKnowledgeBaseStore(KnowledgeBaseStorage):
    """内存版持久化实现：模拟 kb_entries 表行为，可注入故障。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.upsert_calls: list[dict] = []
        self.verification_calls: list[tuple] = []
        self.delete_calls: list[str] = []
        self.delete_all_calls = 0
        self.list_calls = 0
        # 故障注入：置为 Exception 实例后所有写操作抛错
        self.fail_on_write: Exception | None = None
        self.fail_on_list: Exception | None = None

    def _check_write(self):
        if self.fail_on_write is not None:
            raise self.fail_on_write

    def upsert_kb_entry(self, entry: dict) -> None:
        self._check_write()
        self.upsert_calls.append(entry)
        self.rows[entry["fingerprint"]] = dict(entry)

    def update_kb_verification(
        self, fingerprint, verify_count, case_confidence, updated_at
    ) -> bool:
        self._check_write()
        self.verification_calls.append(
            (fingerprint, verify_count, case_confidence, updated_at)
        )
        row = self.rows.get(fingerprint)
        if row is None:
            return False
        row["verify_count"] = verify_count
        row["case_confidence"] = case_confidence
        row["updated_at"] = updated_at
        return True

    def delete_kb_entry(self, fingerprint: str) -> bool:
        self._check_write()
        self.delete_calls.append(fingerprint)
        return self.rows.pop(fingerprint, None) is not None

    def delete_all_kb_entries(self) -> int:
        self._check_write()
        self.delete_all_calls += 1
        count = len(self.rows)
        self.rows.clear()
        return count

    def list_recent_kb_entries(self, limit: int = 100) -> list[dict]:
        if self.fail_on_list is not None:
            raise self.fail_on_list
        self.list_calls += 1
        rows = sorted(self.rows.values(), key=lambda r: r["updated_at"], reverse=True)
        return rows[:limit]


@pytest.fixture
def fake_store(monkeypatch):
    fake = FakeKnowledgeBaseStore()
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: fake)
    return fake


def _upsert(store: KnowledgeBaseStore, fp: str, source: str = "llm"):
    return store.upsert(
        fingerprint=fp,
        analysis={"exception_type": "ValueError", "message": f"boom {fp}"},
        fix_suggestion=f"fix {fp}",
        source=source,
    )


# ---------------------------------------------------------------------------
# 写穿（write-through）
# ---------------------------------------------------------------------------


def test_upsert_writes_through_to_persistent_store(fake_store):
    store = KnowledgeBaseStore(max_entries=10)

    entry = _upsert(store, "fp-1")

    assert len(fake_store.upsert_calls) == 1
    persisted = fake_store.rows["fp-1"]
    assert persisted["fingerprint"] == "fp-1"
    assert persisted["fix_suggestion"] == "fix fp-1"
    assert persisted["source"] == "llm"
    assert persisted["updated_at"] == entry["updated_at"]
    # 归一化/类型指纹同步落库（供排查与未来检索）
    assert persisted["normalized_fingerprint"]
    assert persisted["type_fingerprint"]


def test_upsert_eviction_deletes_from_persistent_store(fake_store):
    store = KnowledgeBaseStore(max_entries=2)

    _upsert(store, "fp-1")
    _upsert(store, "fp-2")
    _upsert(store, "fp-3")  # 触发 LRU 驱逐 fp-1

    assert fake_store.delete_calls == ["fp-1"]
    assert "fp-1" not in fake_store.rows
    assert set(fake_store.rows) == {"fp-2", "fp-3"}
    # 内存与持久层条数一致
    assert store.size() == len(fake_store.rows) == 2


def test_record_verification_writes_through(fake_store):
    store = KnowledgeBaseStore(max_entries=10)
    _upsert(store, "fp-1")

    result = store.record_verification("fp-1", 0.85)

    assert result["verify_count"] == 1
    assert result["case_confidence"] == 0.85
    assert len(fake_store.verification_calls) == 1
    fp, count, confidence, updated_at = fake_store.verification_calls[0]
    assert fp == "fp-1"
    assert count == 1
    assert confidence == 0.85
    assert updated_at == result["updated_at"]
    # 持久层同步更新
    assert fake_store.rows["fp-1"]["verify_count"] == 1


def test_clear_deletes_all_from_persistent_store(fake_store):
    store = KnowledgeBaseStore(max_entries=10)
    _upsert(store, "fp-1")
    _upsert(store, "fp-2")

    store.clear()

    assert fake_store.delete_all_calls == 1
    assert fake_store.rows == {}
    assert store.size() == 0


def test_record_verification_missing_entry_skips_persist(fake_store):
    store = KnowledgeBaseStore(max_entries=10)

    assert store.record_verification("missing", 0.9) is None
    assert fake_store.verification_calls == []


# ---------------------------------------------------------------------------
# 降级：持久层故障不阻断 KB 主流程
# ---------------------------------------------------------------------------


def test_persist_failure_degrades_gracefully(fake_store):
    fake_store.fail_on_write = RuntimeError("pg down")
    store = KnowledgeBaseStore(max_entries=2)

    entry = _upsert(store, "fp-1")  # 不应抛异常
    assert entry["fingerprint"] == "fp-1"
    assert store.get("fp-1") is not None

    store.record_verification("fp-1", 0.9)  # 同样不抛
    assert store.get("fp-1")["verify_count"] == 1

    store.clear()  # clear 也不抛
    assert store.size() == 0


def test_load_failure_degrades_gracefully(fake_store):
    fake_store.fail_on_list = RuntimeError("pg down")
    store = KnowledgeBaseStore(max_entries=10)

    assert store.load_from_persistent() == 0


def test_persistent_store_unavailable_falls_back_to_memory_only(monkeypatch):
    def _raise():
        raise RuntimeError("backend init failed")

    monkeypatch.setattr(kb_module, "get_knowledge_store", _raise)
    store = KnowledgeBaseStore(max_entries=10)

    entry = _upsert(store, "fp-1")  # 不应抛异常
    assert entry["fingerprint"] == "fp-1"
    assert store.size() == 1


# ---------------------------------------------------------------------------
# 启动回灌（load_from_persistent）
# ---------------------------------------------------------------------------


def test_load_from_persistent_restores_entries_with_stats(fake_store):
    now = time.time()
    fake_store.rows["fp-1"] = {
        "fingerprint": "fp-1",
        "analysis": {"root_cause": "db timeout"},
        "fix_suggestion": "add retry",
        "source": "llm",
        "created_at": now - 100,
        "updated_at": now - 50,
        "normalized_fingerprint": "norm-1",
        "type_fingerprint": "type-1",
        "verify_count": 3,
        "case_confidence": 0.9,
    }

    store = KnowledgeBaseStore(max_entries=10)
    count = store.load_from_persistent()

    assert count == 1
    restored = store.get("fp-1")
    assert restored is not None
    assert restored["analysis"]["root_cause"] == "db timeout"
    assert restored["fix_suggestion"] == "add retry"
    assert restored["source"] == "llm"
    assert restored["created_at"] == now - 100
    assert restored["updated_at"] == now - 50
    # 验证统计原值保留
    assert restored["verify_count"] == 3
    assert restored["case_confidence"] == 0.9
    # 三级索引同步重建
    assert store.get_by_normalized_fingerprint("norm-1") is not None
    assert len(store.get_by_type_fingerprint("type-1")) == 1


def test_load_from_persistent_respects_max_entries(fake_store):
    now = time.time()
    for i in range(5):
        fake_store.rows[f"fp-{i}"] = {
            "fingerprint": f"fp-{i}",
            "analysis": {},
            "fix_suggestion": "",
            "source": "llm",
            "created_at": now - 100 + i,
            "updated_at": now - 50 + i,
            "normalized_fingerprint": "",
            "type_fingerprint": "",
            "verify_count": 0,
            "case_confidence": 0.0,
        }

    store = KnowledgeBaseStore(max_entries=3)
    count = store.load_from_persistent()

    assert count == 3
    assert store.size() == 3
    # 保留 updated_at 最新的 3 条（fp-2/3/4），最旧的 fp-0/fp-1 不加载
    assert store.get("fp-4") is not None
    assert store.get("fp-2") is not None
    assert store.get("fp-0") is None


def test_load_from_persistent_preserves_lru_order(fake_store):
    """回灌后 LRU 队首应是最久未更新条目（继续写入时优先驱逐它）。"""
    now = time.time()
    for i, updated_at in enumerate([now - 30, now - 10, now - 20]):
        fake_store.rows[f"fp-{i}"] = {
            "fingerprint": f"fp-{i}",
            "analysis": {},
            "fix_suggestion": "",
            "source": "llm",
            "created_at": updated_at - 100,
            "updated_at": updated_at,
            "normalized_fingerprint": "",
            "type_fingerprint": "",
            "verify_count": 0,
            "case_confidence": 0.0,
        }

    store = KnowledgeBaseStore(max_entries=3)
    store.load_from_persistent()

    # 写入第 4 条触发驱逐：应驱逐 updated_at 最旧的 fp-0
    _upsert(store, "fp-new")
    assert store.get("fp-0") is None
    assert fake_store.delete_calls == ["fp-0"]
    assert store.get("fp-1") is not None
    assert store.get("fp-2") is not None


def test_load_from_persistent_overrides_in_memory_duplicates(fake_store):
    now = time.time()
    store = KnowledgeBaseStore(max_entries=10)
    _upsert(store, "fp-1")  # 内存先有旧版本（写穿进 fake rows）
    # 模拟 PG 中存在更新的权威版本（如上次运行写穿的结果）
    fake_store.rows["fp-1"] = {
        "fingerprint": "fp-1",
        "analysis": {"root_cause": "persisted version"},
        "fix_suggestion": "persisted fix",
        "source": "llm",
        "created_at": now - 100,
        "updated_at": now - 50,
        "normalized_fingerprint": "",
        "type_fingerprint": "",
        "verify_count": 1,
        "case_confidence": 0.5,
    }

    store.load_from_persistent()  # PG 为权威来源覆盖

    entry = store.get("fp-1")
    assert entry["analysis"]["root_cause"] == "persisted version"
    assert entry["verify_count"] == 1


# ---------------------------------------------------------------------------
# memory 后端 no-op 行为
# ---------------------------------------------------------------------------


def test_noop_knowledge_store_is_inert():
    noop = NoOpKnowledgeBaseStore()
    noop.upsert_kb_entry({"fingerprint": "fp"})
    assert noop.update_kb_verification("fp", 1, 0.5, time.time()) is False
    assert noop.delete_kb_entry("fp") is False
    assert noop.delete_all_kb_entries() == 0
    assert noop.list_recent_kb_entries() == []

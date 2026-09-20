"""v0.5.3 KB 持久化（写穿 + 启动回灌）单元测试。

用内存版 FakeKnowledgeBaseStore 经 persist_store 显式注入（AD-1 方案 B），
验证 KnowledgeBaseStore 的写穿行为与回灌行为；SQLite 真实落库由
tests/unit/test_sqlite_kb_store.py 用临时路径覆盖。
"""

import time

import pytest

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
def fake_store():
    """注入用内存版持久化替身（AD-1 方案 B：经 persist_store 显式注入）。"""
    return FakeKnowledgeBaseStore()


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
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)

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
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=2)

    _upsert(store, "fp-1")
    _upsert(store, "fp-2")
    _upsert(store, "fp-3")  # 触发 LRU 驱逐 fp-1

    assert fake_store.delete_calls == ["fp-1"]
    assert "fp-1" not in fake_store.rows
    assert set(fake_store.rows) == {"fp-2", "fp-3"}
    # 内存与持久层条数一致
    assert store.size() == len(fake_store.rows) == 2


def test_upsert_failure_does_not_evict_old_entry(fake_store):
    """B03: 写穿失败时不得删除被驱逐旧条目，旧条目必须仍在内存与持久层。"""
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=2)
    _upsert(store, "fp-1")
    _upsert(store, "fp-2")
    # 此时 LRU 顺序: fp-1 (最旧), fp-2 (最新)

    # 注入写入失败：仅持久化新条目失败，delete 正常可用
    def _fail_upsert(entry):
        raise RuntimeError("disk write error")
    fake_store.upsert_kb_entry = _fail_upsert

    _upsert(store, "fp-3")

    # 必须满足：只有新条目持久化成功才允许删除被驱逐条目；持久化失败时旧条目必须仍在内存与持久层
    assert "fp-1" not in fake_store.delete_calls
    assert "fp-1" in fake_store.rows
    assert store.get("fp-1") is not None


def test_sqlite_upsert_failure_keeps_old_entry_on_restart(tmp_path, monkeypatch):
    """B03 红绿验证：临时 SQLite 故障注入 + 重启回灌，新条目写穿失败不得删除旧条目。"""
    from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore

    db_path = str(tmp_path / "b03_test.sqlite3")
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)

    store = KnowledgeBaseStore(persist_store=sqlite_store, max_entries=2)
    _upsert(store, "fp-1")
    _upsert(store, "fp-2")
    assert len(sqlite_store.list_recent_kb_entries(limit=10)) == 2

    # 注入故障：让 sqlite_store.upsert_kb_entry 抛错
    def _faulty_upsert(entry):
        raise RuntimeError("simulated sqlite fault")

    monkeypatch.setattr(sqlite_store, "upsert_kb_entry", _faulty_upsert)

    _upsert(store, "fp-3")

    # 1. 运行时内存检查：旧条目 fp-1 仍保留在内存中
    assert store.get("fp-1") is not None
    # 2. 持久层检查：旧条目 fp-1 仍在持久层中未被删除
    raw_rows = [r["fingerprint"] for r in sqlite_store.list_recent_kb_entries(limit=10)]
    assert "fp-1" in raw_rows

    # 3. 模拟重启回灌：新实例从持久层恢复，fp-1 完好无损
    store_restart = KnowledgeBaseStore(persist_store=sqlite_store, max_entries=2)
    loaded = store_restart.load_from_persistent()
    assert loaded == 2
    assert store_restart.get("fp-1") is not None
    assert store_restart.get("fp-2") is not None


def test_record_verification_writes_through(fake_store):
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
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
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
    _upsert(store, "fp-1")
    _upsert(store, "fp-2")

    assert store.clear() is True

    assert fake_store.delete_all_calls == 1
    assert fake_store.rows == {}
    assert store.size() == 0


def test_clear_noop_memory_backend_returns_true():
    """约束 1：无持久层（未注入 Store，memory 后端语义）时，clear() 返回 True。"""
    store = KnowledgeBaseStore(max_entries=10)
    _upsert(store, "fp-1")
    assert store.size() == 1

    assert store.clear() is True
    assert store.size() == 0


def test_record_verification_missing_entry_skips_persist(fake_store):
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)

    assert store.record_verification("missing", 0.9) is None
    assert fake_store.verification_calls == []


# ---------------------------------------------------------------------------
# 降级：持久层故障不阻断 KB 主流程
# ---------------------------------------------------------------------------


def test_persist_failure_degrades_gracefully(fake_store):
    fake_store.fail_on_write = RuntimeError("pg down")
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=2)

    entry = _upsert(store, "fp-1")  # 不应抛异常
    assert entry["fingerprint"] == "fp-1"
    assert store.get("fp-1") is not None

    store.record_verification("fp-1", 0.9)  # 同样不抛
    assert store.get("fp-1")["verify_count"] == 1


def test_clear_persist_failure_refuses_clear_and_keeps_memory(fake_store, caplog):
    """决策 6 (U12)：持久层删除失败时，clear() 必须返回 False 且不得清空内存，防止重启复活。"""
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=2)
    _upsert(store, "fp-1")
    assert store.size() == 1

    fake_store.fail_on_write = RuntimeError("storage disk failure")
    with caplog.at_level("WARNING"):
        res = store.clear()

    # 失败必须显式返回 False
    assert res is False
    # 内存必须保留，防止重启死灰复燃
    assert store.size() == 1
    assert store.get("fp-1") is not None
    assert "KB clear failed on persistent store" in caplog.text or "failed" in caplog.text


def test_load_failure_degrades_gracefully(fake_store):
    fake_store.fail_on_list = RuntimeError("pg down")
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)

    assert store.load_from_persistent() == 0


def test_persistent_store_unavailable_falls_back_to_memory_only():
    """注入的 Store 完全故障（方法全抛错）时，KB 主流程不受影响（AD-1 注入语义迁移）。"""

    class _BrokenStore(FakeKnowledgeBaseStore):
        def upsert_kb_entry(self, entry):
            raise RuntimeError("backend init failed")

    store = KnowledgeBaseStore(persist_store=_BrokenStore(), max_entries=10)

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

    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
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

    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=3)
    count = store.load_from_persistent()

    assert count == 3
    assert store.size() == 3
    # 保留 updated_at 最新的 3 条（fp-2/3/4），最旧的 fp-0/fp-1 不加载
    assert store.get("fp-4") is not None
    assert store.get("fp-2") is not None
    assert store.get("fp-0") is None


def test_load_from_persistent_orders_eviction_by_updated_at(fake_store):
    """回灌按 updated_at 恢复顺序；无后续访问调整时，继续写入优先驱逐最久未更新条目。本测试不验证跨重启保留访问顺序。"""
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

    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=3)
    store.load_from_persistent()

    # 写入第 4 条触发驱逐：应驱逐 updated_at 最旧的 fp-0
    _upsert(store, "fp-new")
    assert store.get("fp-0") is None
    assert fake_store.delete_calls == ["fp-0"]
    assert store.get("fp-1") is not None
    assert store.get("fp-2") is not None


def test_load_from_persistent_overrides_in_memory_duplicates(fake_store):
    now = time.time()
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
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


# ---------------------------------------------------------------------------
# S7：知识库存储边界拒写未脱敏内容
# ---------------------------------------------------------------------------


_REDACTION_REJECTION_MARKER = "KB_UNREDACTED_CONTENT_REJECTED"


def _has_items(items) -> bool:
    """以布尔值检查拦截副作用，避免失败输出打印未脱敏样本。"""
    return bool(items)


class _RecordingVectorStore:
    """记录 KB 边界是否把内容送入向量索引。"""

    def __init__(self):
        self.docs: list[dict] = []

    def add(self, docs):
        self.docs.extend(docs)


def test_unredacted_upsert_is_rejected_before_persistence_and_vector_write(
    fake_store, monkeypatch, caplog
):
    """未脱敏写入应在任何存储副作用前拒绝，并留下不含原文的固定标记。"""
    vector_store = _RecordingVectorStore()
    monkeypatch.setattr(
        "app.rag.knowledge_base.get_vector_store", lambda: vector_store
    )
    monkeypatch.setattr("app.rag.knowledge_base.settings.kb_vector_index_autosync", True)
    monkeypatch.setattr("app.rag.knowledge_base.settings.redaction_enabled", True)
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)

    with caplog.at_level("WARNING"):
        try:
            store.upsert(
                fingerprint="fp-unredacted",
                analysis={"message": 'password = "secret123"'},
                fix_suggestion="safe fix",
                source="llm",
            )
        except Exception:
            # 边界可用异常或既有跳过路径拒写；下方断言验证外部契约。
            pass

    has_memory_write = store.get("fp-unredacted") is not None
    has_persistent_write = _has_items(fake_store.upsert_calls)
    has_vector_write = _has_items(vector_store.docs)
    assert not has_memory_write
    assert not has_persistent_write
    assert not has_vector_write
    assert _REDACTION_REJECTION_MARKER in caplog.text
    assert "secret123" not in caplog.text


def test_pre_redacted_upsert_is_stored_byte_identically_when_enabled(
    fake_store, monkeypatch, caplog
):
    """已脱敏内容在默认开关下应作为不动点原样写入持久层和向量索引。"""
    vector_store = _RecordingVectorStore()
    monkeypatch.setattr(
        "app.rag.knowledge_base.get_vector_store", lambda: vector_store
    )
    monkeypatch.setattr("app.rag.knowledge_base.settings.kb_vector_index_autosync", True)
    monkeypatch.setattr("app.rag.knowledge_base.settings.redaction_enabled", True)
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
    masked = {
        "message": 'password="***"',
        "phone": "*******PHONE*******",
    }

    with caplog.at_level("WARNING"):
        result = store.upsert(
            fingerprint="fp-masked",
            analysis=masked,
            fix_suggestion="safe fix",
            source="llm",
        )

    assert fake_store.rows["fp-masked"]["analysis"] == masked
    assert vector_store.docs[0]["analysis"] == masked
    assert result["analysis"] == masked
    assert _REDACTION_REJECTION_MARKER not in caplog.text


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('password = "secret123"', True),
        ('password="***"', False),
        ({"analysis": [{"message": 'password = "secret123"'}]}, True),
        ('{"analysis":[{"message":"password = \\"secret123\\""}]}', True),
    ],
    ids=["raw-value", "masked-value", "nested-dict-list", "json-string"],
)
def test_unredacted_secret_detector_uses_fixed_point_contract(payload, expected):
    """检测器只判定内置规则替换前后是否相同，并递归检查字符串叶子。"""
    from app.utils import pattern_guard

    detector = getattr(pattern_guard, "contains_unredacted_secret", None)
    assert callable(detector)
    assert detector(payload) is expected


def test_verification_miss_fallback_rejects_unredacted_persistent_write(
    fake_store, caplog
):
    """verification miss 直调持久层 upsert 时也必须拒绝并安全留痕。"""
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
    unsafe_entry = {
        "fingerprint": "fp-verification-unsafe",
        "analysis": {"message": 'password = "secret123"'},
        "fix_suggestion": "safe fix",
        "source": "llm",
        "verify_count": 1,
        "case_confidence": 0.9,
        "updated_at": time.time(),
    }

    with caplog.at_level("WARNING"):
        store._persist_verification(unsafe_entry)

    assert not fake_store.verification_calls
    has_upsert_fallback = _has_items(fake_store.upsert_calls)
    has_persisted_row = _has_items(fake_store.rows)
    assert not has_upsert_fallback
    assert not has_persisted_row
    assert _REDACTION_REJECTION_MARKER in caplog.text
    assert "secret123" not in caplog.text


@pytest.mark.parametrize("sync_all", [False, True], ids=["single-entry", "full-sync"])
def test_vector_sync_rejects_unredacted_entries(
    sync_all, fake_store, monkeypatch, caplog
):
    """单条与回灌后全量向量同步均不得接收未脱敏条目。"""
    from app.rag.knowledge_base import KnowledgeBaseStore

    vector_store = _RecordingVectorStore()
    monkeypatch.setattr(
        "app.rag.knowledge_base.get_vector_store", lambda: vector_store
    )
    monkeypatch.setattr("app.rag.knowledge_base.settings.redaction_enabled", True)
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
    unsafe_entry = {
        "fingerprint": "fp-vector-unsafe",
        "analysis": {"message": 'password = "secret123"'},
        "fix_suggestion": "safe fix",
        "source": "llm",
    }

    if sync_all:
        fake_store.rows[unsafe_entry["fingerprint"]] = {
            **unsafe_entry,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        assert store.load_from_persistent() == 1
        with caplog.at_level("WARNING"):
            store._sync_all_to_vector_store()
    else:
        with caplog.at_level("WARNING"):
            store._sync_entry_to_vector_store(unsafe_entry)

    has_vector_write = _has_items(vector_store.docs)
    assert not has_vector_write
    assert _REDACTION_REJECTION_MARKER in caplog.text
    assert "secret123" not in caplog.text


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
def test_redaction_switch_controls_kb_write_rejection(
    enabled, fake_store, monkeypatch, caplog
):
    """开关开启时拒写，显式关闭时保持原有写入行为且无拒写日志。"""
    vector_store = _RecordingVectorStore()
    monkeypatch.setattr(
        "app.rag.knowledge_base.get_vector_store", lambda: vector_store
    )
    monkeypatch.setattr("app.rag.knowledge_base.settings.kb_vector_index_autosync", True)
    monkeypatch.setattr("app.rag.knowledge_base.settings.redaction_enabled", enabled)
    store = KnowledgeBaseStore(persist_store=fake_store, max_entries=10)
    raw_analysis = {"message": 'password = "secret123"'}

    with caplog.at_level("WARNING"):
        if enabled:
            try:
                store.upsert(
                    fingerprint="fp-switch",
                    analysis=raw_analysis,
                    fix_suggestion="safe fix",
                    source="llm",
                )
            except Exception:
                # 边界拒写可以抛异常或走已有跳过路径。
                pass
            has_persistent_write = _has_items(fake_store.upsert_calls)
            has_vector_write = _has_items(vector_store.docs)
            assert not has_persistent_write
            assert not has_vector_write
            assert _REDACTION_REJECTION_MARKER in caplog.text
            assert "secret123" not in caplog.text
        else:
            result = store.upsert(
                fingerprint="fp-switch",
                analysis=raw_analysis,
                fix_suggestion="safe fix",
                source="llm",
            )
            assert result["analysis"] == raw_analysis
            assert fake_store.rows["fp-switch"]["analysis"] == raw_analysis
            assert vector_store.docs[0]["analysis"] == raw_analysis
            assert _REDACTION_REJECTION_MARKER not in caplog.text

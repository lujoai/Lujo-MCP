"""B06: seed 重放保护测试。

验证：
1. max_entries 满容量重启时不被 seed 挤出既有学习经验；
2. seed 重放不得覆盖已有的验证统计（verify_count, case_confidence）；
3. 首次冷启动时仍正常加载全部种子。
"""
import pytest

from app.config import settings
import app.rag.knowledge_base as kb_module
from app.rag.knowledge_base import KnowledgeBaseStore
from app.rag.seed_data import SEED_CASES
from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore


def _upsert_learned(store: KnowledgeBaseStore, fp: str):
    return store.upsert(
        fingerprint=fp,
        analysis={"exception_type": "LearnedError", "message": f"msg {fp}"},
        fix_suggestion=f"fix {fp}",
        source="llm",
    )


def test_seed_does_not_evict_learned_entries_when_full(tmp_path, monkeypatch):
    """B06 红测试：满容量重启后，seed 重放不得挤掉既有学习经验。"""
    db_path = str(tmp_path / "b06_full.sqlite3")
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)
    monkeypatch.setattr(settings, "kb_persist_enabled", True)
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: sqlite_store)

    # 设容量上限为 3
    store = KnowledgeBaseStore(max_entries=3)
    _upsert_learned(store, "fp-learned-1")
    _upsert_learned(store, "fp-learned-2")
    _upsert_learned(store, "fp-learned-3")
    assert store.size() == 3

    # 模拟重启：新实例回灌持久经验
    store_restarted = KnowledgeBaseStore(max_entries=3)
    loaded = store_restarted.load_from_persistent()
    assert loaded == 3
    assert store_restarted.get("fp-learned-1") is not None

    # 重启后加载种子数据
    store_restarted.load_seed_cases(SEED_CASES)

    # 验证：3 条学习经验一条都不能丢！
    assert store_restarted.get("fp-learned-1") is not None
    assert store_restarted.get("fp-learned-2") is not None
    assert store_restarted.get("fp-learned-3") is not None
    assert store_restarted.size() == 3


def test_seed_does_not_overwrite_existing_verification_stats(tmp_path, monkeypatch):
    """B06 红测试：已有条目的验证统计（verify_count 等）不得被 seed 重放重置。"""
    db_path = str(tmp_path / "b06_stats.sqlite3")
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)
    monkeypatch.setattr(settings, "kb_persist_enabled", True)
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: sqlite_store)

    seed_fp = SEED_CASES[0]["fingerprint"]
    store = KnowledgeBaseStore(max_entries=10)

    # 先以 seed 插入一条
    store.load_seed_cases([SEED_CASES[0]])
    # 模拟经过多次运行时验证强化
    store.record_verification(seed_fp, confidence=0.92)
    store.record_verification(seed_fp, confidence=0.95)

    entry_before = store.get(seed_fp)
    assert entry_before["verify_count"] == 2
    assert entry_before["case_confidence"] == 0.95

    # 模拟再次调用 load_seed_cases
    store.load_seed_cases(SEED_CASES)

    entry_after = store.get(seed_fp)
    # 统计必须保留，不得被重置为 seed 默认值
    assert entry_after["verify_count"] == 2
    assert entry_after["case_confidence"] == 0.95


def test_cold_start_loads_all_seeds(monkeypatch):
    """B06: 首次冷启动（空库且容量充足）仍正常加载全部种子。"""
    store = KnowledgeBaseStore(max_entries=100)
    assert store.size() == 0

    loaded = store.load_seed_cases(SEED_CASES)
    assert loaded == len(SEED_CASES)
    assert store.size() == len(SEED_CASES)

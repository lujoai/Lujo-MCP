"""v0.8.0「笔记本」：SQLiteKnowledgeBaseStore 与工厂分发测试。

覆盖：
1. KnowledgeBaseStorage ABC 契约（5 方法行为与 PG 实现对齐）；
2. 跨实例持久性（模拟进程重启：同一文件两个独立实例）；
3. 端到端真实链路：KnowledgeBaseStore 写穿 → 新实例回灌 → 经验命中 + 三级索引重建；
4. 工厂分发：KB_PERSIST_ENABLED=true → SQLite / false → NoOp / 初始化失败 → 降级 NoOp；
5. 不污染：全部使用 tmp_path 隔离文件。

注：tests/unit/conftest.py 已显式关闭 kb_persist_enabled（保持既有用例行为不变），
需要开启的用例在用例内 monkeypatch 并指向临时路径。
"""
from pathlib import Path

import pytest

import app.rag.knowledge_base as kb_module
from app.rag.knowledge_base import KnowledgeBaseStore
from app.runtime.core.storage import factory as storage_factory
from app.runtime.core.storage.base import KnowledgeBaseStorage
from app.runtime.core.storage.noop_store import NoOpKnowledgeBaseStore
from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "kb-test.sqlite3")


@pytest.fixture
def store(db_path) -> SQLiteKnowledgeBaseStore:
    return SQLiteKnowledgeBaseStore(db_path=db_path)


def _entry(fingerprint: str, *, updated_at: float = 1000.0, **overrides) -> dict:
    entry = {
        "fingerprint": fingerprint,
        "analysis": {
            "exception_type": "TypeError",
            "message": "unsupported operand type(s) for +: 'int' and 'str'",
            "root_cause": "对 int 与 str 执行 + 运算，类型不匹配",
        },
        "fix_suggestion": "统一操作数类型",
        "source": "llm",
        "created_at": updated_at - 10,
        "updated_at": updated_at,
        "normalized_fingerprint": "norm-fp",
        "type_fingerprint": "type-fp",
        "verify_count": 0,
        "case_confidence": 0.0,
    }
    entry.update(overrides)
    return entry


# ── 1. ABC 契约 ──────────────────────────────────────────────────────


def test_implements_knowledge_base_storage_abc(store):
    assert isinstance(store, KnowledgeBaseStorage)


def test_upsert_then_list_round_trip(store):
    store.upsert_kb_entry(_entry("fp-1", updated_at=1000.0))

    rows = store.list_recent_kb_entries(limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["fingerprint"] == "fp-1"
    # analysis JSON round-trip（含中文与嵌套结构）
    assert row["analysis"]["root_cause"] == "对 int 与 str 执行 + 运算，类型不匹配"
    assert row["analysis"]["exception_type"] == "TypeError"
    assert row["fix_suggestion"] == "统一操作数类型"
    assert row["source"] == "llm"
    assert row["created_at"] == 1000.0 - 10
    assert row["updated_at"] == 1000.0
    assert row["normalized_fingerprint"] == "norm-fp"
    assert row["type_fingerprint"] == "type-fp"
    assert row["verify_count"] == 0
    assert row["case_confidence"] == 0.0


def test_upsert_same_fingerprint_updates_in_place(store):
    store.upsert_kb_entry(_entry("fp-1", updated_at=1000.0))
    store.upsert_kb_entry(
        _entry("fp-1", updated_at=2000.0, fix_suggestion="新修法", created_at=999.0)
    )

    rows = store.list_recent_kb_entries(limit=10)

    assert len(rows) == 1
    assert rows[0]["fix_suggestion"] == "新修法"
    assert rows[0]["updated_at"] == 2000.0


def test_list_recent_orders_by_updated_at_desc_and_respects_limit(store):
    for i, ts in enumerate([1000.0, 3000.0, 2000.0]):
        store.upsert_kb_entry(_entry(f"fp-{i}", updated_at=ts))

    rows = store.list_recent_kb_entries(limit=2)

    assert [r["fingerprint"] for r in rows] == ["fp-1", "fp-2"]  # 3000, 2000


def test_update_verification_hit_and_miss(store):
    store.upsert_kb_entry(_entry("fp-1"))

    assert store.update_kb_verification("fp-1", 3, 0.9, 5000.0) is True
    assert store.update_kb_verification("missing", 1, 0.5, 5000.0) is False

    row = store.list_recent_kb_entries(limit=1)[0]
    assert row["verify_count"] == 3
    assert row["case_confidence"] == 0.9
    assert row["updated_at"] == 5000.0


def test_delete_one_and_delete_all(store):
    store.upsert_kb_entry(_entry("fp-1"))
    store.upsert_kb_entry(_entry("fp-2"))

    assert store.delete_kb_entry("fp-1") is True
    assert store.delete_kb_entry("fp-1") is False
    assert [r["fingerprint"] for r in store.list_recent_kb_entries()] == ["fp-2"]

    assert store.delete_all_kb_entries() == 1
    assert store.list_recent_kb_entries() == []


def test_malformed_analysis_json_degrades_to_empty_dict(store, db_path):
    """analysis 列内容损坏时读回空 dict，不抛异常（防御性）。"""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO kb_entries (fingerprint, analysis, updated_at) VALUES (?, ?, ?)",
            ("bad-json", "{not-json", 1.0),
        )

    rows = store.list_recent_kb_entries()

    assert len(rows) == 1
    assert rows[0]["analysis"] == {}


# ── 2. 跨实例持久性（模拟进程重启） ──────────────────────────────────


def test_data_survives_new_instance_same_file(db_path):
    SQLiteKnowledgeBaseStore(db_path=db_path).upsert_kb_entry(_entry("fp-1"))

    # 新实例 = 模拟下一个进程打开同一文件
    reopened = SQLiteKnowledgeBaseStore(db_path=db_path)
    rows = reopened.list_recent_kb_entries()

    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "fp-1"
    assert rows[0]["analysis"]["root_cause"]


def test_schema_init_is_idempotent(db_path):
    SQLiteKnowledgeBaseStore(db_path=db_path).upsert_kb_entry(_entry("fp-1"))

    # 二次初始化不破坏既有数据
    store2 = SQLiteKnowledgeBaseStore(db_path=db_path)

    assert len(store2.list_recent_kb_entries()) == 1


# ── 3. 端到端：写穿 → 回灌 → 命中（真实链路） ────────────────────────


def test_write_through_then_reload_restores_hit(monkeypatch, db_path):
    """真实链路：KnowledgeBaseStore 写穿 SQLite → 新实例回灌 → 经验可命中。"""
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: sqlite_store)

    # 第一次运行：分析结果沉淀（写穿）
    first = KnowledgeBaseStore(max_entries=10)
    first.upsert(
        fingerprint="fp-e2e",
        analysis={
            "exception_type": "TypeError",
            "message": "unsupported operand type(s) for +: 'int' and 'str'",
            "root_cause": "类型不匹配",
        },
        fix_suggestion="先做类型转换",
        source="llm",
    )
    first.record_verification("fp-e2e", 0.9)

    # 模拟重启：新进程实例从同一文件回灌
    second = KnowledgeBaseStore(max_entries=10)
    restored = second.load_from_persistent()

    assert restored == 1
    hit = second.get("fp-e2e")
    assert hit is not None
    assert hit["fix_suggestion"] == "先做类型转换"
    assert hit["verify_count"] == 1
    assert hit["case_confidence"] == 0.9
    # 三级索引同步重建 → 归一化/类型级命中仍可用
    assert second.get_by_normalized_fingerprint(hit["normalized_fingerprint"]) is not None
    assert len(second.get_by_type_fingerprint(hit["type_fingerprint"])) == 1


def test_write_through_eviction_and_clear_sync_to_sqlite(monkeypatch, db_path):
    """LRU 驱逐与 clear 同步删除落库条目（内存与笔记本一致）。"""
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: sqlite_store)

    store = KnowledgeBaseStore(max_entries=2)
    for fp in ("fp-1", "fp-2", "fp-3"):  # 驱逐 fp-1
        store.upsert(
            fingerprint=fp,
            analysis={"exception_type": "E", "message": fp},
            fix_suggestion="f",
            source="llm",
        )

    # 驱逐同步删除（fp-1 不在）；顺序依赖真实时间戳，不在此断言
    assert sorted(r["fingerprint"] for r in sqlite_store.list_recent_kb_entries()) == [
        "fp-2",
        "fp-3",
    ]

    store.clear()
    assert sqlite_store.list_recent_kb_entries() == []


def test_default_path_from_settings(monkeypatch, tmp_path):
    """未显式传路径时读 settings.kb_persist_path，并解析为绝对路径。"""
    target = tmp_path / "from-settings.sqlite3"
    monkeypatch.setattr("app.config.settings.kb_persist_path", str(target))

    store = SQLiteKnowledgeBaseStore()

    assert Path(store.db_path) == target.resolve()
    store.upsert_kb_entry(_entry("fp-1"))
    assert target.exists()


def test_relative_path_resolved_against_constructor_cwd(monkeypatch, tmp_path):
    """相对路径在构造时定格为绝对路径（运行期 cwd 变化不影响落库位置）。"""
    import os

    monkeypatch.chdir(tmp_path)
    store = SQLiteKnowledgeBaseStore(db_path="relative-kb.sqlite3")

    assert Path(store.db_path).is_absolute()
    assert Path(store.db_path) == (tmp_path / "relative-kb.sqlite3").resolve()
    # 切到别处后写入仍落在构造时的位置
    other = tmp_path / "other"
    other.mkdir()
    os.chdir(other)
    try:
        store.upsert_kb_entry(_entry("fp-1"))
    finally:
        os.chdir(tmp_path)
    assert (tmp_path / "relative-kb.sqlite3").exists()


def test_memory_path_rejected_explicitly(tmp_path):
    """':memory:' 必须显式拒绝（短连接下每连接独立内存库，无法持久化）。"""
    with pytest.raises(ValueError, match=":memory:"):
        SQLiteKnowledgeBaseStore(db_path=":memory:")


def test_connections_released_after_operations(db_path, tmp_path):
    """操作后连接应已显式关闭：Windows 下未关闭的连接会阻止文件删除。"""
    store = SQLiteKnowledgeBaseStore(db_path=db_path)
    store.upsert_kb_entry(_entry("fp-1"))
    store.list_recent_kb_entries()
    store.update_kb_verification("fp-1", 1, 0.5, 2000.0)
    store.delete_all_kb_entries()

    # 能删除主文件（及 WAL 伴生文件）即证明连接未悬挂
    Path(db_path).unlink()
    assert not Path(db_path).exists()


# ── 4. 工厂分发 ──────────────────────────────────────────────────────


def test_factory_returns_sqlite_store_when_enabled(monkeypatch, db_path):
    monkeypatch.setattr("app.config.settings.storage_backend", "memory")
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", True)
    monkeypatch.setattr("app.config.settings.kb_persist_path", db_path)
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)

    store = storage_factory.get_knowledge_store()

    assert isinstance(store, SQLiteKnowledgeBaseStore)
    assert Path(store.db_path) == Path(db_path).resolve()


def test_factory_returns_noop_when_disabled(monkeypatch, db_path):
    monkeypatch.setattr("app.config.settings.storage_backend", "memory")
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", False)
    monkeypatch.setattr("app.config.settings.kb_persist_path", db_path)
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)

    store = storage_factory.get_knowledge_store()

    assert isinstance(store, NoOpKnowledgeBaseStore)
    # 关闭时不产生任何文件（含 WAL 伴生文件）
    assert not Path(db_path).exists()


def test_factory_degrades_to_noop_on_sqlite_init_failure(monkeypatch, db_path):
    """SQLite 初始化失败（如路径不可写）→ 降级 NoOp，不阻断启动。"""
    monkeypatch.setattr("app.config.settings.storage_backend", "memory")
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", True)
    # 显式指向临时路径：即便将来实现改为模块级 import 使 monkeypatch 失效，
    # 也不会在仓库工作目录落盘（防自产物）
    monkeypatch.setattr("app.config.settings.kb_persist_path", db_path)
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)

    attempts = {"count": 0}

    class _Boom(SQLiteKnowledgeBaseStore):
        def __init__(self, db_path=None):  # noqa: ARG002
            attempts["count"] += 1
            raise OSError("path not writable")

    monkeypatch.setattr(
        "app.runtime.core.storage.sqlite_kb_store.SQLiteKnowledgeBaseStore", _Boom
    )

    store = storage_factory.get_knowledge_store()

    assert isinstance(store, NoOpKnowledgeBaseStore)
    # 防假阴性：必须真的尝试过 SQLite 初始化（否则「整段分支被删」也会绿）
    assert attempts["count"] == 1, "工厂应尝试 SQLite 初始化后才能降级"
    assert not Path(db_path).exists()


def test_factory_wired_end_to_end_write_through_and_reload(monkeypatch, db_path):
    """真实工厂分发链路的端到端：工厂返回的 store 落盘 → 模拟重启回灌命中。

    与 test_write_through_then_reload_restores_hit 的区别：本用例**不 monkeypatch
    kb_module.get_knowledge_store**，完全经真实 factory 分发（monkeypatch 仅用于
    配置与单例重置）。若工厂的 KB_PERSIST 分支被删/写反，本用例必红。
    """
    monkeypatch.setattr("app.config.settings.storage_backend", "memory")
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", True)
    monkeypatch.setattr("app.config.settings.kb_persist_path", db_path)
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)

    # 第一次运行：经真实工厂拿 store 写穿
    first = KnowledgeBaseStore(max_entries=10)
    first.upsert(
        fingerprint="fp-factory-e2e",
        analysis={
            "exception_type": "TypeError",
            "message": "unsupported operand type(s)",
            "root_cause": "类型不匹配",
        },
        fix_suggestion="先做类型转换",
        source="llm",
    )
    assert Path(db_path).exists(), "工厂分发的 SQLite store 应真实落盘"

    # 模拟进程重启：重置工厂单例与 KB 实例，从同一文件回灌
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)
    second = KnowledgeBaseStore(max_entries=10)

    assert second.load_from_persistent() == 1
    hit = second.get("fp-factory-e2e")
    assert hit is not None
    assert hit["fix_suggestion"] == "先做类型转换"
    assert second.get_by_normalized_fingerprint(hit["normalized_fingerprint"]) is not None


def test_pg_backend_branch_untouched(monkeypatch):
    """STORAGE_BACKEND=postgresql 分发路径不变（仍走 PG store，不受 KB_PERSIST 影响）。"""
    monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
    monkeypatch.setattr("app.config.settings.pg_async_enabled", False)
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", True)
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)

    import app.runtime.core.storage.pg_kb_store as pg_kb_module

    store = storage_factory.get_knowledge_store()

    assert isinstance(store, pg_kb_module.PGKnowledgeBaseStore)


# ── 5. 不污染工作目录 ────────────────────────────────────────────────


def test_custom_path_only_creates_that_file(db_path, tmp_path):
    store = SQLiteKnowledgeBaseStore(db_path=db_path)
    store.upsert_kb_entry(_entry("fp-1"))

    created = sorted(p.name for p in tmp_path.iterdir())
    # WAL 模式会额外产生 -wal/-shm 伴生文件，但全部落在指定临时目录内
    assert created, "应至少创建 sqlite 主文件"
    assert all(name.startswith("kb-test.sqlite3") for name in created), created

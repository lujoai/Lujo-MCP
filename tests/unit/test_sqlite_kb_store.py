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

from app.rag.knowledge_base import KnowledgeBaseStore
from app.runtime.core.storage import factory as storage_factory
from app.runtime.core.storage import sqlite_kb_store as sqlite_store_module
from app.runtime.core.storage.base import KnowledgeBaseStorage
from app.runtime.core.storage.noop_store import NoOpKnowledgeBaseStore
from app.runtime.core.storage.sqlite_kb_store import (
    SQLiteKnowledgeBaseStore,
    _migrate_legacy_cwd_kb_if_needed,
    get_default_kb_persist_path,
    is_valid_sqlite_kb,
)


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


def test_write_through_then_reload_restores_hit(db_path):
    """真实链路：注入 SQLite Store 写穿 → 新实例回灌 → 经验可命中。"""
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)

    # 第一次运行：分析结果沉淀（写穿）
    first = KnowledgeBaseStore(persist_store=sqlite_store, max_entries=10)
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
    second = KnowledgeBaseStore(persist_store=sqlite_store, max_entries=10)
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


def test_write_through_eviction_and_clear_sync_to_sqlite(db_path):
    """LRU 驱逐与 clear 同步删除落库条目（内存与笔记本一致）。"""
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)

    store = KnowledgeBaseStore(persist_store=sqlite_store, max_entries=2)
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

    与 test_write_through_then_reload_restores_hit 的区别：本用例**不直接构造
    SQLite store**，完全经真实 factory 分发并按 AD-1 方案 B 装配语义注入
    （monkeypatch 仅用于配置与单例重置）。若工厂的 KB_PERSIST 分支被删/写反，
    本用例必红。
    """
    monkeypatch.setattr("app.config.settings.storage_backend", "memory")
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", True)
    monkeypatch.setattr("app.config.settings.kb_persist_path", db_path)
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)

    # 第一次运行：经真实工厂拿 store 注入写穿（Composition Root 装配语义）
    first = KnowledgeBaseStore(
        persist_store=storage_factory.get_knowledge_store(), max_entries=10
    )
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
    second = KnowledgeBaseStore(
        persist_store=storage_factory.get_knowledge_store(), max_entries=10
    )

    assert second.load_from_persistent() == 1
    hit = second.get("fp-factory-e2e")
    assert hit is not None
    assert hit["fix_suggestion"] == "先做类型转换"
    assert second.get_by_normalized_fingerprint(hit["normalized_fingerprint"]) is not None


def test_pg_backend_rejected_not_dispatched(monkeypatch):
    """WP3：STORAGE_BACKEND=postgresql 被拒绝，不再分发到 PGKnowledgeBaseStore。

    关键不变量：``kb_persist_enabled=True`` **不得**把拒绝救回来——KB store 的
    SQLite / NoOp 分支位于 ``_validate_backend()`` 之后，后端被拒绝时任何本地
    持久化开关都不能让进程照常启动（否则等价于静默回退）。
    """
    monkeypatch.setattr("app.config.settings.storage_backend", "postgresql")
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", True)
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)

    with pytest.raises(
        storage_factory.StorageBackendRemovedError, match="移除"
    ) as exc_info:
        storage_factory.get_knowledge_store()

    msg = str(exc_info.value)
    assert "migrate_pg_kb_to_sqlite" in msg, "拒绝信息必须给一次性迁移指引"
    assert "SQLite" in msg
    # 单例不得被静默降级实例污染（SQLite / NoOp 都不允许写入缓存）
    assert storage_factory._knowledge_store is None


# ── 5. 不污染工作目录 ────────────────────────────────────────────────


def test_custom_path_only_creates_that_file(db_path, tmp_path):
    store = SQLiteKnowledgeBaseStore(db_path=db_path)
    store.upsert_kb_entry(_entry("fp-1"))

    created = sorted(p.name for p in tmp_path.iterdir())
    # WAL 模式会额外产生 -wal/-shm 伴生文件，但全部落在指定临时目录内
    assert created, "应至少创建 sqlite 主文件"
    assert all(name.startswith("kb-test.sqlite3") for name in created), created


# ── 6. 默认用户数据目录与非破坏性安全迁移（跨项目经验沉淀）───────────


def test_default_kb_path_windows_with_localappdata(monkeypatch, tmp_path):
    """Windows 下若 LOCALAPPDATA 存在，定位到 %LOCALAPPDATA%/lujo-mcp/lujo-kb.sqlite3。"""
    fake_appdata = tmp_path / "AppData" / "Local"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))

    p = get_default_kb_persist_path()

    assert p == (fake_appdata / "lujo-mcp" / "lujo-kb.sqlite3").resolve()


def test_default_kb_path_windows_fallback(monkeypatch, tmp_path):
    """Windows 下若 LOCALAPPDATA 未设，回退到 ~/.local/share/lujo-mcp/lujo-kb.sqlite3。"""
    fake_home = tmp_path / "home"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    p = get_default_kb_persist_path()

    assert p == (fake_home / ".local" / "share" / "lujo-mcp" / "lujo-kb.sqlite3").resolve()


def test_default_kb_path_macos(monkeypatch, tmp_path):
    """macOS 下定位到 ~/Library/Application Support/lujo-mcp/lujo-kb.sqlite3。"""
    fake_home = tmp_path / "home"
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    p = get_default_kb_persist_path()

    assert p == (fake_home / "Library" / "Application Support" / "lujo-mcp" / "lujo-kb.sqlite3").resolve()


def test_default_kb_path_linux_with_xdg(monkeypatch, tmp_path):
    """Linux 下若 XDG_DATA_HOME 存在，定位到 $XDG_DATA_HOME/lujo-mcp/lujo-kb.sqlite3。"""
    fake_xdg = tmp_path / "custom_xdg"
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_xdg))

    p = get_default_kb_persist_path()

    assert p == (fake_xdg / "lujo-mcp" / "lujo-kb.sqlite3").resolve()


def test_default_kb_path_linux_fallback(monkeypatch, tmp_path):
    """Linux 下若 XDG_DATA_HOME 未设，回退到 ~/.local/share/lujo-mcp/lujo-kb.sqlite3。"""
    fake_home = tmp_path / "home"
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    p = get_default_kb_persist_path()

    assert p == (fake_home / ".local" / "share" / "lujo-mcp" / "lujo-kb.sqlite3").resolve()


def test_store_uses_default_data_dir_when_unconfigured(monkeypatch, tmp_path):
    """未传 db_path 且 settings.kb_persist_path 为空时，自动定位并初始化默认用户数据目录。"""
    fake_appdata = tmp_path / "AppData" / "Local"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")
    isolated_cwd = tmp_path / "empty_cwd"
    isolated_cwd.mkdir()
    monkeypatch.chdir(isolated_cwd)

    store = SQLiteKnowledgeBaseStore()

    expected_path = (fake_appdata / "lujo-mcp" / "lujo-kb.sqlite3").resolve()
    assert Path(store.db_path) == expected_path
    assert expected_path.exists()
    assert (expected_path.parent / ".lujo-kb-cwd-migration-complete").is_file()


def test_explicit_constructor_arg_takes_precedence_over_all(monkeypatch, tmp_path):
    """构造函数显式传入 db_path 必须 100% 优先，不使用默认目录且不做迁移。"""
    fake_appdata = tmp_path / "AppData" / "Local"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))
    monkeypatch.setattr("app.config.settings.kb_persist_path", str(tmp_path / "from-settings.sqlite3"))

    explicit_target = tmp_path / "explicit" / "custom.sqlite3"
    store = SQLiteKnowledgeBaseStore(db_path=str(explicit_target))

    assert Path(store.db_path) == explicit_target.resolve()
    assert explicit_target.exists()
    assert not (fake_appdata / "lujo-mcp" / "lujo-kb.sqlite3").exists()


def test_explicit_settings_kb_persist_path_takes_precedence_over_default(monkeypatch, tmp_path):
    """显式设置 settings.kb_persist_path 时优先使用该路径，不使用默认目录。"""
    fake_appdata = tmp_path / "AppData" / "Local"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))

    settings_target = tmp_path / "settings_explicit" / "custom.sqlite3"
    monkeypatch.setattr("app.config.settings.kb_persist_path", str(settings_target))

    store = SQLiteKnowledgeBaseStore()

    assert Path(store.db_path) == settings_target.resolve()
    assert settings_target.exists()
    assert not (fake_appdata / "lujo-mcp" / "lujo-kb.sqlite3").exists()


def test_explicit_path_does_not_trigger_cwd_migration(monkeypatch, tmp_path):
    """显式配置路径时不触发 CWD 自动迁移（不进行隐式复制）。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    legacy_file = cwd_dir / "lujo-kb.sqlite3"
    legacy_store = SQLiteKnowledgeBaseStore(db_path=str(legacy_file))
    legacy_store.upsert_kb_entry(_entry("fp-cwd-legacy"))

    explicit_file = tmp_path / "explicit" / "isolated.sqlite3"
    store = SQLiteKnowledgeBaseStore(db_path=str(explicit_file))

    assert store.list_recent_kb_entries() == []
    assert legacy_file.exists()


def test_migration_copies_valid_cwd_file_and_preserves_original(monkeypatch, tmp_path):
    """非破坏性安全迁移：CWD 存在有效 lujo-kb.sqlite3 时安全复制到用户数据目录，原文件保留且数据完整。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    legacy_file = cwd_dir / "lujo-kb.sqlite3"
    legacy_store = SQLiteKnowledgeBaseStore(db_path=str(legacy_file))
    legacy_store.upsert_kb_entry(_entry("fp-migrated", fix_suggestion="migrated fix"))

    userdata_dir = tmp_path / "userdata"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(userdata_dir))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")

    target_file = userdata_dir / "lujo-mcp" / "lujo-kb.sqlite3"
    assert not target_file.exists(), "迁移前目标数据库不应存在"

    store = SQLiteKnowledgeBaseStore()

    # 1. 目标文件被创建
    assert target_file.exists()
    assert Path(store.db_path) == target_file.resolve()
    assert (target_file.parent / ".lujo-kb-cwd-migration-complete").is_file()

    # 2. 原文件绝对保留（非破坏性）
    assert legacy_file.exists()

    # 3. 目标数据完整可读
    rows = store.list_recent_kb_entries()
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "fp-migrated"
    assert rows[0]["fix_suggestion"] == "migrated fix"

    # 4. 原文件数据依然完整可读
    reopened_legacy = SQLiteKnowledgeBaseStore(db_path=str(legacy_file))
    legacy_rows = reopened_legacy.list_recent_kb_entries()
    assert len(legacy_rows) == 1
    assert legacy_rows[0]["fingerprint"] == "fp-migrated"


def test_deleted_default_kb_does_not_reimport_preserved_cwd_file(monkeypatch, tmp_path):
    """用户停服后只删新笔记本即可重置，保留的 CWD 原件不能在下次启动复活。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    legacy_file = cwd_dir / "lujo-kb.sqlite3"
    SQLiteKnowledgeBaseStore(db_path=str(legacy_file)).upsert_kb_entry(_entry("old"))

    userdata_dir = tmp_path / "userdata"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(userdata_dir))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")
    target_file = userdata_dir / "lujo-mcp" / "lujo-kb.sqlite3"
    marker = target_file.parent / ".lujo-kb-cwd-migration-complete"

    first = SQLiteKnowledgeBaseStore()
    assert [row["fingerprint"] for row in first.list_recent_kb_entries()] == ["old"]
    assert marker.is_file()

    target_file.unlink()
    restarted = SQLiteKnowledgeBaseStore()
    assert restarted.list_recent_kb_entries() == []
    assert marker.is_file()
    assert SQLiteKnowledgeBaseStore(db_path=str(legacy_file)).list_recent_kb_entries()[0]["fingerprint"] == "old"


def test_migration_uses_consistent_snapshot_with_live_wal(monkeypatch, tmp_path):
    """存在活动 WAL 时通过 SQLite backup 迁移最新提交数据，不复制瞬态 sidecar。"""
    import sqlite3

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    legacy_file = cwd_dir / "lujo-kb.sqlite3"
    legacy_store = SQLiteKnowledgeBaseStore(db_path=str(legacy_file))
    wal_file = cwd_dir / "lujo-kb.sqlite3-wal"
    keeper = sqlite3.connect(str(legacy_file))
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        legacy_store.upsert_kb_entry(_entry("fp-wal", fix_suggestion="committed in WAL"))
        assert wal_file.is_file(), "保持连接打开时，WAL 文件应仍存在"

        target_file = tmp_path / "target" / "lujo-kb.sqlite3"
        migrated = _migrate_legacy_cwd_kb_if_needed(target_file)

        assert migrated is True
        assert is_valid_sqlite_kb(target_file)
        target_store = SQLiteKnowledgeBaseStore(db_path=str(target_file))
        rows = target_store.list_recent_kb_entries()
        assert len(rows) == 1
        assert rows[0]["fingerprint"] == "fp-wal"
        assert rows[0]["fix_suggestion"] == "committed in WAL"

        # 迁移后的目标是完整快照，不依赖复制过来的 WAL/SHM 文件。
        assert not (tmp_path / "target" / "lujo-kb.sqlite3-wal").exists()
        assert legacy_file.exists()
        assert is_valid_sqlite_kb(legacy_file)
    finally:
        keeper.close()


def test_sqlite_validation_never_falls_back_to_writable_connection(monkeypatch, tmp_path):
    """只读 URI 连接失败时，校验应失败且不得改用可写连接。"""
    candidate = tmp_path / "candidate.sqlite3"
    SQLiteKnowledgeBaseStore(db_path=str(candidate))
    original_bytes = candidate.read_bytes()
    connect_calls = []

    def _fail_readonly(database, *args, **kwargs):
        connect_calls.append((database, kwargs))
        raise OSError("simulated read-only connection failure")

    monkeypatch.setattr(sqlite_store_module.sqlite3, "connect", _fail_readonly)

    assert is_valid_sqlite_kb(candidate) is False
    assert len(connect_calls) == 1
    database, kwargs = connect_calls[0]
    assert database.endswith("?mode=ro")
    assert kwargs.get("uri") is True
    assert candidate.read_bytes() == original_bytes


def test_migration_publish_failure_leaves_no_partial_target(monkeypatch, tmp_path):
    """快照发布失败时不留下会阻止后续迁移的半成品目标文件。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    legacy_file = cwd_dir / "lujo-kb.sqlite3"
    legacy_store = SQLiteKnowledgeBaseStore(db_path=str(legacy_file))
    legacy_store.upsert_kb_entry(_entry("fp-preserved"))

    target_file = tmp_path / "target" / "lujo-kb.sqlite3"

    def _fail_link(_source, _target):
        raise OSError("simulated atomic publish failure")

    with monkeypatch.context() as failure:
        failure.setattr(sqlite_store_module.os, "link", _fail_link)
        with pytest.raises(OSError, match="simulated atomic publish failure"):
            _migrate_legacy_cwd_kb_if_needed(target_file)
    assert not target_file.exists()
    assert not (target_file.parent / ".lujo-kb-cwd-migration-complete").exists()
    assert is_valid_sqlite_kb(legacy_file)
    assert SQLiteKnowledgeBaseStore(db_path=str(legacy_file)).list_recent_kb_entries()[0]["fingerprint"] == "fp-preserved"
    assert list(target_file.parent.glob(".lujo-kb.sqlite3.migrate-*.tmp")) == []

    assert _migrate_legacy_cwd_kb_if_needed(target_file) is True
    assert SQLiteKnowledgeBaseStore(db_path=str(target_file)).list_recent_kb_entries()[0]["fingerprint"] == "fp-preserved"


def test_failed_default_migration_degrades_then_retries(monkeypatch, tmp_path):
    """有效旧库迁移失败不能用空目标占位；下次仍能经 Factory 迁移。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    legacy_file = cwd_dir / "lujo-kb.sqlite3"
    SQLiteKnowledgeBaseStore(db_path=str(legacy_file)).upsert_kb_entry(_entry("old"))

    userdata_dir = tmp_path / "userdata"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(userdata_dir))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")
    monkeypatch.setattr("app.config.settings.kb_persist_enabled", True)
    monkeypatch.setattr("app.config.settings.storage_backend", "memory")
    monkeypatch.setattr(storage_factory, "_knowledge_store", None)
    target_file = userdata_dir / "lujo-mcp" / "lujo-kb.sqlite3"

    def _fail_link(_source, _target):
        raise OSError("publish failed")

    with monkeypatch.context() as failure:
        failure.setattr(sqlite_store_module.os, "link", _fail_link)
        assert isinstance(storage_factory.get_knowledge_store(), NoOpKnowledgeBaseStore)
    assert not target_file.exists()
    assert not (target_file.parent / ".lujo-kb-cwd-migration-complete").exists()
    assert is_valid_sqlite_kb(legacy_file)

    monkeypatch.setattr(storage_factory, "_knowledge_store", None)
    retried = storage_factory.get_knowledge_store()
    assert isinstance(retried, SQLiteKnowledgeBaseStore)
    assert [row["fingerprint"] for row in retried.list_recent_kb_entries()] == ["old"]


def test_default_marker_write_failure_is_explicit(monkeypatch, tmp_path):
    """标记不能保存时不可宣称默认持久化已就绪。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    userdata_dir = tmp_path / "userdata"
    target_file = userdata_dir / "lujo-mcp" / "lujo-kb.sqlite3"
    SQLiteKnowledgeBaseStore(db_path=str(target_file)).upsert_kb_entry(_entry("existing"))
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(userdata_dir))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")

    def _fail_fsync(_fd):
        raise OSError("marker unwritable")

    with monkeypatch.context() as failure:
        failure.setattr(sqlite_store_module.os, "fsync", _fail_fsync)
        with pytest.raises(OSError, match="marker unwritable"):
            SQLiteKnowledgeBaseStore()
    assert not (target_file.parent / ".lujo-kb-cwd-migration-complete").exists()
    assert SQLiteKnowledgeBaseStore().list_recent_kb_entries()[0]["fingerprint"] == "existing"


def test_migration_does_not_overwrite_existing_target(monkeypatch, tmp_path):
    """目标位置已存在数据库时不被 CWD 旧文件覆盖（避免覆盖已有用户数据目录数据）。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    legacy_file = cwd_dir / "lujo-kb.sqlite3"
    legacy_store = SQLiteKnowledgeBaseStore(db_path=str(legacy_file))
    legacy_store.upsert_kb_entry(_entry("fp-cwd"))

    userdata_dir = tmp_path / "userdata"
    target_file = userdata_dir / "lujo-mcp" / "lujo-kb.sqlite3"
    target_store = SQLiteKnowledgeBaseStore(db_path=str(target_file))
    target_store.upsert_kb_entry(_entry("fp-target"))

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(userdata_dir))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")

    store = SQLiteKnowledgeBaseStore()

    rows = store.list_recent_kb_entries()
    fps = [r["fingerprint"] for r in rows]
    assert "fp-target" in fps
    assert "fp-cwd" not in fps
    assert legacy_file.exists()
    marker = target_file.parent / ".lujo-kb-cwd-migration-complete"
    assert marker.is_file(), "旧版已存在的默认目标应补记迁移状态"

    target_file.unlink()
    restarted = SQLiteKnowledgeBaseStore()
    assert restarted.list_recent_kb_entries() == []
    assert marker.is_file()


def test_migration_skips_corrupted_cwd_file(monkeypatch, tmp_path):
    """CWD 下的文件损坏（非合法 SQLite 格式）时不被迁移，目标库正常初始化为空库。"""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    corrupt_file = cwd_dir / "lujo-kb.sqlite3"
    corrupt_file.write_text("invalid corrupt header text that is definitely not sqlite")

    userdata_dir = tmp_path / "userdata"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(userdata_dir))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")

    store = SQLiteKnowledgeBaseStore()

    target_file = userdata_dir / "lujo-mcp" / "lujo-kb.sqlite3"
    assert target_file.exists()
    assert store.list_recent_kb_entries() == []
    assert corrupt_file.exists()


def test_migration_skips_sqlite_file_without_kb_entries(monkeypatch, tmp_path):
    """CWD 下存在有效 SQLite 库但无 kb_entries 表时不被迁移，避免误拷不相关数据库。"""
    import sqlite3

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    other_db = cwd_dir / "lujo-kb.sqlite3"
    conn = sqlite3.connect(str(other_db))
    conn.execute("CREATE TABLE other_table (id INT)")
    conn.commit()
    conn.close()

    userdata_dir = tmp_path / "userdata"
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(userdata_dir))
    monkeypatch.setattr("app.config.settings.kb_persist_path", "")

    store = SQLiteKnowledgeBaseStore()

    target_file = userdata_dir / "lujo-mcp" / "lujo-kb.sqlite3"
    assert target_file.exists()
    assert store.list_recent_kb_entries() == []
    assert other_db.exists()


def test_migration_skips_incomplete_kb_entries_schema(monkeypatch, tmp_path):
    """只有部分 kb_entries 字段的 SQLite 文件不得被当作旧 KB 迁移。"""
    import sqlite3

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    legacy_db = cwd_dir / "lujo-kb.sqlite3"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        "CREATE TABLE kb_entries (fingerprint TEXT PRIMARY KEY, analysis TEXT)"
    )
    conn.commit()
    conn.close()

    target_db = tmp_path / "userdata" / "lujo-mcp" / "lujo-kb.sqlite3"
    assert is_valid_sqlite_kb(legacy_db) is False
    assert _migrate_legacy_cwd_kb_if_needed(target_db) is False
    assert not target_db.exists()
    assert legacy_db.exists()


def test_sqlite_validation_rejects_kb_entries_without_fingerprint_primary_key(
    tmp_path,
):
    """必需列齐全但缺少 fingerprint 主键时，写入冲突契约不安全，必须拒绝。"""
    import sqlite3

    candidate = tmp_path / "without-primary-key.sqlite3"
    conn = sqlite3.connect(str(candidate))
    conn.execute(
        """
        CREATE TABLE kb_entries (
            fingerprint TEXT,
            analysis TEXT,
            fix_suggestion TEXT,
            source TEXT,
            created_at REAL,
            updated_at REAL,
            normalized_fingerprint TEXT,
            type_fingerprint TEXT,
            verify_count INTEGER DEFAULT 0,
            case_confidence REAL DEFAULT 0.0
        )
        """
    )
    conn.commit()
    conn.close()

    assert is_valid_sqlite_kb(candidate) is False


def test_is_valid_sqlite_kb_helper(tmp_path):
    """is_valid_sqlite_kb 校验辅助函数的单元测试边界。"""
    import sqlite3

    # 不存在
    assert is_valid_sqlite_kb(tmp_path / "nonexistent.sqlite3") is False

    # 空文件
    empty_file = tmp_path / "empty.sqlite3"
    empty_file.write_bytes(b"")
    assert is_valid_sqlite_kb(empty_file) is False

    # 小于 100 字节
    tiny_file = tmp_path / "tiny.sqlite3"
    tiny_file.write_bytes(b"SQLite format 3\x00short")
    assert is_valid_sqlite_kb(tiny_file) is False

    # 非 SQLite 魔数
    fake_header = tmp_path / "fake_header.sqlite3"
    fake_header.write_bytes(b"A" * 200)
    assert is_valid_sqlite_kb(fake_header) is False

    # 合法 SQLite 但无 kb_entries 表
    unrelated_db = tmp_path / "unrelated.sqlite3"
    conn = sqlite3.connect(str(unrelated_db))
    conn.execute("CREATE TABLE dummy (x TEXT)")
    conn.commit()
    conn.close()
    assert is_valid_sqlite_kb(unrelated_db) is False

    # 合法 SQLite 且含 kb_entries 表
    valid_db = tmp_path / "valid.sqlite3"
    SQLiteKnowledgeBaseStore(db_path=str(valid_db))
    assert is_valid_sqlite_kb(valid_db) is True

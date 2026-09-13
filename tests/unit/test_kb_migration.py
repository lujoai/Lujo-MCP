"""WP2（Step 3）：factory 一次性迁移入口 migrate_knowledge_entries() 的注入式单测。

覆盖（Step 3 设计文档 §4.2 契约 / §4.3 五道护栏 / §4.4 十一条边界）：
1. 护栏：_MIGRATION_SOURCE_BACKENDS 与 _VALID_BACKENDS 物理分离（G3）、无运行时调用方（G4）、
   只返回 report dict 不返回 store（G2）、KnowledgeBaseStorage ABC 零改动；
2. 边界：B1 limit 直传 list_recent_kb_entries、B4 十字段一一对应、B5 重复执行幂等、
   B6 冲突策略 skip/upsert、B7 空表非失败、B8 fail_fast 与逐行隔离、B9 目标备份与保护、
   B10 report 不含凭据；
3. 不迁移面：traces / errors / sessions / specs 的 PG store 在迁移全程不被构造；
4. 迁移循环只经存储边界：源只读（list_recent_kb_entries）、目标只写（upsert_kb_entry + checkpoint）；
5. CLI 零数据库代码：AST import 白名单 + SQL/driver 令牌静态扫描 + 参数映射与退出码。

注入方式：factory 在函数体内延迟 import 两个具体 store 类，测试用 monkeypatch 替换
模块属性注入假源/假目标（与 test_factory.py / test_sqlite_kb_store.py 同一手法）。
全程不连接真实 PostgreSQL、不读写 .env。
"""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest

import app.runtime.core.storage.factory as f
from app.runtime.core.storage.base import KnowledgeBaseStorage
from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_SCRIPT = REPO_ROOT / "scripts" / "migrate_pg_kb_to_sqlite.py"

_KB_FIELDS = {
    "fingerprint",
    "analysis",
    "fix_suggestion",
    "source",
    "created_at",
    "updated_at",
    "normalized_fingerprint",
    "type_fingerprint",
    "verify_count",
    "case_confidence",
}


def _entry(fingerprint: str, *, updated_at: float = 1000.0, **overrides) -> dict:
    """构造十字段齐全的 KB entry（模拟 PG 读边界 _parse_data 之后的产物）。"""
    entry = {
        "fingerprint": fingerprint,
        "analysis": {
            "exception_type": "TypeError",
            "message": f"msg-{fingerprint}",
            "root_cause": "类型不匹配",
        },
        "fix_suggestion": f"fix-{fingerprint}",
        "source": "llm",
        "created_at": updated_at - 10,
        "updated_at": updated_at,
        "normalized_fingerprint": f"norm-{fingerprint}",
        "type_fingerprint": f"type-{fingerprint}",
        "verify_count": 1,
        "case_confidence": 0.5,
    }
    entry.update(overrides)
    return entry


def _make_source_class(entries, name="FakePGKnowledgeBaseStore"):
    """假 PG 源：只允许被读取；任何写方法被调用即失败（迁移全程不写源库，B10）。"""

    class FakePGKnowledgeBaseStore:
        constructed = 0
        list_calls: list[int] = []

        def __init__(self):
            type(self).constructed += 1

        def list_recent_kb_entries(self, limit: int = 100) -> list[dict]:
            type(self).list_calls.append(limit)
            return [dict(e) for e in entries[: max(limit, 0)]]

        def upsert_kb_entry(self, entry: dict) -> None:
            raise AssertionError("迁移不得写源库（B10）")

        def update_kb_verification(self, *args, **kwargs) -> bool:
            raise AssertionError("迁移不得写源库（B10）")

        def delete_kb_entry(self, fingerprint: str) -> bool:
            raise AssertionError("迁移不得写源库（B10）")

        def delete_all_kb_entries(self) -> int:
            raise AssertionError("迁移不得写源库（B10）")

    FakePGKnowledgeBaseStore.__name__ = name
    return FakePGKnowledgeBaseStore


def _make_target_class(seed=(), fail_on=frozenset(), name="FakeSQLiteKnowledgeBaseStore"):
    """假 SQLite 目标：记录边界方法调用；fail_on 中的 fingerprint 写入即抛错。"""
    rows = [dict(e) for e in seed]

    class FakeSQLiteKnowledgeBaseStore:
        constructed = 0
        method_calls: list[str] = []

        def __init__(self, db_path=None):
            type(self).constructed += 1
            self.db_path = db_path

        def upsert_kb_entry(self, entry: dict) -> None:
            type(self).method_calls.append("upsert_kb_entry")
            fp = entry.get("fingerprint")
            if fp in fail_on:
                raise RuntimeError(f"injected upsert failure: {fp}")
            rows.append(dict(entry))

        def list_recent_kb_entries(self, limit: int = 100) -> list[dict]:
            type(self).method_calls.append("list_recent_kb_entries")
            return [dict(e) for e in rows]

        def checkpoint(self) -> None:
            type(self).method_calls.append("checkpoint")

        def update_kb_verification(self, *args, **kwargs) -> bool:
            type(self).method_calls.append("update_kb_verification")
            raise AssertionError("迁移不得回写验证统计")

        def delete_kb_entry(self, fingerprint: str) -> bool:
            type(self).method_calls.append("delete_kb_entry")
            raise AssertionError("迁移不得删除目标数据")

        def delete_all_kb_entries(self) -> int:
            type(self).method_calls.append("delete_all_kb_entries")
            raise AssertionError("迁移不得清空目标数据")

    FakeSQLiteKnowledgeBaseStore.__name__ = name
    return FakeSQLiteKnowledgeBaseStore


def _install_source(monkeypatch, source_cls) -> None:
    monkeypatch.setattr(
        "app.runtime.core.storage.pg_kb_store.PGKnowledgeBaseStore", source_cls
    )


def _install_stores(monkeypatch, source_cls, target_cls) -> None:
    _install_source(monkeypatch, source_cls)
    monkeypatch.setattr(
        "app.runtime.core.storage.sqlite_kb_store.SQLiteKnowledgeBaseStore", target_cls
    )


def _migrate(monkeypatch, entries, target_path, *, target_cls=None, **kwargs) -> dict:
    """便捷入口：注入假源后调用 factory.migrate_knowledge_entries。

    默认不替换目标 store —— 走真实 SQLiteKnowledgeBaseStore（tmp_path 落盘），
    用于验证真实落盘/备份/幂等；需要失败注入或调用记录时显式传 target_cls。
    """
    _install_source(monkeypatch, kwargs.pop("source_cls", None) or _make_source_class(entries))
    if target_cls is not None:
        monkeypatch.setattr(
            "app.runtime.core.storage.sqlite_kb_store.SQLiteKnowledgeBaseStore", target_cls
        )
    return f.migrate_knowledge_entries(
        source_backend="postgresql", target_path=str(target_path), **kwargs
    )


@pytest.fixture(autouse=True)
def _protect_runtime_state(monkeypatch):
    """迁移全程不得触碰运行时单例（G3/G4 的前置保护，防自产物）。"""
    sentinels = {
        "_trace_store": object(),
        "_session_store": object(),
        "_error_store": object(),
        "_spec_store": object(),
        "_knowledge_store": object(),
    }
    for attr, sentinel in sentinels.items():
        monkeypatch.setattr(f, attr, sentinel)
    yield
    for attr in sentinels:
        setattr(f, attr, None)


# ── 1. 五道护栏（设计文档 §4.3） ─────────────────────────────────────


class TestGuardrails:
    def test_migration_whitelist_is_distinct_object_from_runtime_whitelist(self):
        """G3：_MIGRATION_SOURCE_BACKENDS 与 _VALID_BACKENDS 必须是两个独立集合对象。"""
        assert f._MIGRATION_SOURCE_BACKENDS == {"postgresql"}
        assert f._MIGRATION_SOURCE_BACKENDS is not f._VALID_BACKENDS

    def test_invalid_source_backend_raises(self, monkeypatch, tmp_path):
        """非法 source_backend → ValueError，且在任何 store 构造之前失败。"""
        source_cls = _make_source_class([_entry("fp-1")])
        target_cls = _make_target_class()
        _install_stores(monkeypatch, source_cls, target_cls)

        for bad in ("sqlite", "postgres", "", "memory", "POSTGRESQL"):
            with pytest.raises(ValueError, match="source_backend"):
                f.migrate_knowledge_entries(
                    source_backend=bad, target_path=str(tmp_path / "t.sqlite3")
                )

        assert source_cls.constructed == 0, "校验失败时不得构造任何 store"
        assert target_cls.constructed == 0

    def test_memory_is_runtime_valid_but_not_a_migration_source(self):
        """白名单语义隔离：memory 是运行时合法后端，但绝不能成为迁移源。"""
        assert "memory" in f._VALID_BACKENDS
        assert "memory" not in f._MIGRATION_SOURCE_BACKENDS
        assert "postgresql" in f._MIGRATION_SOURCE_BACKENDS

    def test_migration_works_after_runtime_whitelist_narrowing(self, monkeypatch, tmp_path):
        """G3：模拟 WP3 把 _VALID_BACKENDS 收窄为 {"memory"}，迁移入口不受影响。"""
        monkeypatch.setattr(f, "_VALID_BACKENDS", {"memory"})
        monkeypatch.setattr("app.config.settings.storage_backend", "memory")

        report = _migrate(
            monkeypatch, [_entry("fp-1")], tmp_path / "t.sqlite3", dry_run=True
        )

        assert report["status"] == "ok"
        assert report["read"] == 1

    def test_migration_does_not_call_runtime_validate_backend(self, monkeypatch, tmp_path):
        """迁移入口不得复用运行时 _validate_backend()（其语义属于 STORAGE_BACKEND 链路）。"""

        def _boom():
            raise AssertionError("迁移入口不得调用运行时 _validate_backend()")

        monkeypatch.setattr(f, "_validate_backend", _boom)
        report = _migrate(
            monkeypatch, [_entry("fp-1")], tmp_path / "t.sqlite3", dry_run=True
        )
        assert report["status"] == "ok"

    def test_runtime_singletons_untouched_by_migration(self, monkeypatch, tmp_path):
        """迁移不得触碰 _knowledge_store 等运行时单例（§4.2 十步流程的「临时 store」要求）。"""
        report = _migrate(monkeypatch, [_entry("fp-1")], tmp_path / "t.sqlite3")
        assert report["status"] == "ok"
        # _protect_runtime_state 的 sentinel 仍在原位（未被替换为真实 store）
        assert not isinstance(f._knowledge_store, KnowledgeBaseStorage)

    def test_entry_returns_report_dict_only(self, monkeypatch, tmp_path):
        """G2：返回值必须是纯 dict（JSON 可序列化），不含任何 store 实例。"""
        report = _migrate(
            monkeypatch, [_entry("fp-1"), _entry("fp-2")], tmp_path / "t.sqlite3"
        )
        assert isinstance(report, dict)
        serialized = json.dumps(report, ensure_ascii=False)
        assert "migrated" in serialized
        # store 实例不可 JSON 序列化；序列化成功即证明 report 中没有 store
        assert report["status"] == "ok"

    def test_no_runtime_caller_in_app_tree(self):
        """G4：app/ 下不得出现迁移入口的调用方（factory.py 自身除外）。"""
        hits = []
        for py in sorted((REPO_ROOT / "app").rglob("*.py")):
            text = py.read_text(encoding="utf-8")
            if "migrate_knowledge_entries" in text:
                hits.append(py)
        assert hits == [REPO_ROOT / "app" / "runtime" / "core" / "storage" / "factory.py"], hits

    def test_knowledge_base_abc_unchanged(self):
        """ABC 零改动：抽象方法集仍为原五方法，base.py 不出现迁移/checkpoint 概念。"""
        assert set(KnowledgeBaseStorage.__abstractmethods__) == {
            "upsert_kb_entry",
            "update_kb_verification",
            "delete_kb_entry",
            "delete_all_kb_entries",
            "list_recent_kb_entries",
        }
        base_text = (
            REPO_ROOT / "app" / "runtime" / "core" / "storage" / "base.py"
        ).read_text(encoding="utf-8")
        assert "migrate" not in base_text.lower()

    def test_report_contains_no_credentials(self, monkeypatch, tmp_path):
        """B10：report 输出不含任何凭据或连接串（对齐 test_config_pg_warning 手法）。"""
        report = _migrate(monkeypatch, [_entry("fp-1")], tmp_path / "t.sqlite3")
        serialized = json.dumps(report, ensure_ascii=False)
        for marker in ("password", "postgres://", "postgresql://", "api_key", "redis_url"):
            assert marker not in serialized, marker


# ── 2. 存储边界（B1/B2/B3/B4，不迁移面） ─────────────────────────────


class TestStorageBoundary:
    def test_only_kb_boundary_methods_are_used(self, monkeypatch, tmp_path):
        """迁移循环只经 list_recent_kb_entries（读）与 upsert_kb_entry/checkpoint（写）。"""
        entries = [_entry(f"fp-{i}") for i in range(3)]
        source_cls = _make_source_class(entries)
        target_cls = _make_target_class()
        _install_stores(monkeypatch, source_cls, target_cls)

        f.migrate_knowledge_entries(
            source_backend="postgresql", target_path=str(tmp_path / "t.sqlite3")
        )

        assert len(source_cls.list_calls) == 1, "源侧只做一次全量读取"
        assert "upsert_kb_entry" in target_cls.method_calls
        assert "checkpoint" in target_cls.method_calls
        for forbidden in (
            "update_kb_verification",
            "delete_kb_entry",
            "delete_all_kb_entries",
        ):
            assert forbidden not in target_cls.method_calls

    def test_migration_never_constructs_non_kb_pg_stores(self, monkeypatch, tmp_path):
        """traces / errors / sessions / specs 的 PG store 在迁移全程不得被构造。"""
        for path in (
            "app.runtime.core.storage.pg_trace_store.PGTraceStore",
            "app.runtime.core.storage.pg_session_store.PGSessionStore",
            "app.runtime.core.storage.pg_error_store.PGErrorStore",
            "app.runtime.core.storage.pg_spec_store.PGSpecStore",
            "app.runtime.core.storage.async_pg_store.AsyncPGErrorStore",
        ):

            def _boom(*args, **kwargs):
                raise AssertionError(f"迁移不得构造 {path}")

            monkeypatch.setattr(path, _boom)

        report = _migrate(monkeypatch, [_entry("fp-1")], tmp_path / "t.sqlite3")
        assert report["status"] == "ok"

    def test_field_mapping_ten_fields_passthrough(self, monkeypatch, tmp_path):
        """B4：十字段一一对应、无增无减；迁移层原样搬运 dict，不改写字段。"""
        entry = _entry("fp-map")
        source_cls = _make_source_class([entry])
        captured: list[dict] = []

        class RecordingTarget(_make_target_class()):
            def upsert_kb_entry(self, item: dict) -> None:
                captured.append(dict(item))
                super().upsert_kb_entry(item)

        _install_stores(monkeypatch, source_cls, RecordingTarget)
        f.migrate_knowledge_entries(
            source_backend="postgresql", target_path=str(tmp_path / "t.sqlite3")
        )

        assert len(captured) == 1
        assert set(captured[0].keys()) == _KB_FIELDS
        assert captured[0] == entry, "迁移层不得改写任何字段值"

    def test_jsonb_to_text_conversion_happens_inside_stores(self, monkeypatch, tmp_path):
        """B3：JSONB→dict 已由 PG 读边界完成，dict→TEXT 由 SQLite 写边界完成；
        factory 不复制任何序列化逻辑——嵌套 analysis 经真实 SQLite store round-trip 不变。"""
        nested = {"exception_type": "E", "nested": {"k": [1, 2, 3]}, "cn": "中文根因"}
        entries = [_entry("fp-json", analysis=nested)]
        source_cls = _make_source_class(entries)
        _install_stores(
            monkeypatch,
            source_cls,
            SQLiteKnowledgeBaseStore,  # 真实 SQLite 目标
        )
        db = tmp_path / "t.sqlite3"

        report = f.migrate_knowledge_entries(
            source_backend="postgresql", target_path=str(db)
        )

        assert report["status"] == "ok"
        rows = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()
        assert len(rows) == 1
        assert rows[0]["analysis"] == nested


# ── 3. dry_run（B9 联动） ────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_does_not_create_target(self, monkeypatch, tmp_path):
        """dry_run + 目标不存在 → 不创建目标文件（也不产生 WAL 伴生文件）。"""
        db = tmp_path / "new.sqlite3"
        report = _migrate(monkeypatch, [_entry("fp-1")], db, dry_run=True)

        assert report["dry_run"] is True
        assert report["read"] == 1
        assert report["migrated"] == 1
        assert not db.exists()
        assert list(tmp_path.iterdir()) == [], "dry_run 不得在目标目录留下任何文件"

    def test_dry_run_does_not_modify_existing_target(self, monkeypatch, tmp_path):
        """dry_run + 目标已存在 → 既有行保持原值，不新增行；报告给出「将会发生」的计数。"""
        db = tmp_path / "t.sqlite3"
        store = SQLiteKnowledgeBaseStore(db_path=str(db))
        store.upsert_kb_entry(_entry("fp-a", updated_at=1.0, fix_suggestion="旧修法"))

        report = _migrate(
            monkeypatch,
            [_entry("fp-a", updated_at=2.0, fix_suggestion="新修法"), _entry("fp-b")],
            db,
            dry_run=True,
        )

        assert report["dry_run"] is True
        assert report["read"] == 2
        assert report["migrated"] == 1, "只应报告将会写入的 fp-b"
        assert report["skipped_existing"] == 1
        assert report["errors"] == []

        rows = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()
        assert [r["fingerprint"] for r in rows] == ["fp-a"]
        assert rows[0]["fix_suggestion"] == "旧修法", "dry_run 不得改写既有经验"
        assert rows[0]["updated_at"] == 1.0

    def test_dry_run_creates_no_backup(self, monkeypatch, tmp_path):
        """dry_run + backup=True + 目标已存在 → 不得生成备份文件。"""
        db = tmp_path / "t.sqlite3"
        SQLiteKnowledgeBaseStore(db_path=str(db)).upsert_kb_entry(_entry("fp-a"))

        report = _migrate(monkeypatch, [_entry("fp-b")], db, dry_run=True, backup=True)

        assert report["backup_path"] is None
        assert [p.name for p in tmp_path.iterdir()] == ["t.sqlite3"]


# ── 4. 空表与 limit（B7/B1） ─────────────────────────────────────────


class TestEmptyAndLimit:
    def test_empty_source_is_success_not_failure(self, monkeypatch, tmp_path):
        """B7：源表为空 → migrated=0、status=ok，不视为失败；不创建目标文件。"""
        db = tmp_path / "t.sqlite3"
        report = _migrate(monkeypatch, [], db)

        assert report["status"] == "ok"
        assert report["read"] == 0
        assert report["migrated"] == 0
        assert report["errors"] == []
        assert not db.exists(), "空迁移不应产生目标文件"

    def test_limit_caps_source_read(self, monkeypatch, tmp_path):
        """B1：limit 显式直传 list_recent_kb_entries（非启动回灌的 max_entries 截断）。"""
        entries = [_entry(f"fp-{i}", updated_at=1000.0 + i) for i in range(5)]
        source_cls = _make_source_class(entries)
        target_cls = _make_target_class()
        _install_stores(monkeypatch, source_cls, target_cls)

        report = f.migrate_knowledge_entries(
            source_backend="postgresql", target_path=str(tmp_path / "t.sqlite3"), limit=2
        )

        assert source_cls.list_calls == [2], "limit 必须原样传给读边界"
        assert report["read"] == 2
        assert report["migrated"] == 2

    @pytest.mark.parametrize("bad_limit", [0, -1])
    def test_limit_must_be_positive(self, monkeypatch, tmp_path, bad_limit):
        with pytest.raises(ValueError, match="limit"):
            _migrate(monkeypatch, [_entry("fp-1")], tmp_path / "t.sqlite3", limit=bad_limit)


# ── 5. 冲突与幂等（B5/B6） ───────────────────────────────────────────


class TestConflictAndIdempotency:
    def _setup_existing_target(self, tmp_path):
        db = tmp_path / "t.sqlite3"
        SQLiteKnowledgeBaseStore(db_path=str(db)).upsert_kb_entry(
            _entry("fp-a", updated_at=1.0, fix_suggestion="旧修法")
        )
        return db

    def test_conflict_skip_preserves_existing_experience(self, monkeypatch, tmp_path):
        """B6：默认 skip——目标库既有经验原样保留，绝不按时间戳覆盖。"""
        db = self._setup_existing_target(tmp_path)
        report = _migrate(
            monkeypatch,
            [_entry("fp-a", updated_at=2.0, fix_suggestion="新修法"), _entry("fp-b")],
            db,
        )

        assert report["status"] == "ok"
        assert report["skipped_existing"] == 1
        assert report["migrated"] == 1
        rows = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()
        by_fp = {r["fingerprint"]: r for r in rows}
        assert by_fp["fp-a"]["fix_suggestion"] == "旧修法"
        assert by_fp["fp-b"]["fix_suggestion"] == "fix-fp-b"

    def test_conflict_upsert_overwrites_explicitly(self, monkeypatch, tmp_path):
        """B6：显式 on_conflict=upsert 才允许源覆盖目标（用户显式选择）。"""
        db = self._setup_existing_target(tmp_path)
        report = _migrate(
            monkeypatch, [_entry("fp-a", updated_at=2.0, fix_suggestion="新修法")], db,
            on_conflict="upsert",
        )

        assert report["migrated"] == 1
        assert report["skipped_existing"] == 0
        rows = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()
        assert rows[0]["fix_suggestion"] == "新修法"
        assert rows[0]["updated_at"] == 2.0

    def test_invalid_on_conflict_raises(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="on_conflict"):
            _migrate(
                monkeypatch, [_entry("fp-1")], tmp_path / "t.sqlite3", on_conflict="overwrite"
            )

    def test_rerun_with_upsert_is_idempotent(self, monkeypatch, tmp_path):
        """B5：重复执行（upsert 策略）——第二次结果与第一次一致，不产生重复行。"""
        db = tmp_path / "t.sqlite3"
        entries = [_entry(f"fp-{i}") for i in range(3)]

        first = _migrate(monkeypatch, entries, db, on_conflict="upsert")
        second = _migrate(monkeypatch, entries, db, on_conflict="upsert")

        assert first["migrated"] == 3
        assert second["migrated"] == 3
        assert second["skipped_existing"] == 0
        rows = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()
        assert len(rows) == 3, "重复执行不得产生重复行"

    def test_rerun_with_skip_migrates_nothing(self, monkeypatch, tmp_path):
        """B5：重复执行（skip 策略）——第二次全部命中既有指纹，零写入。"""
        db = tmp_path / "t.sqlite3"
        entries = [_entry(f"fp-{i}") for i in range(3)]

        first = _migrate(monkeypatch, entries, db)
        second = _migrate(monkeypatch, entries, db)

        assert first["migrated"] == 3
        assert second["status"] == "ok"
        assert second["read"] == 3
        assert second["migrated"] == 0
        assert second["skipped_existing"] == 3

    def test_duplicate_fingerprints_within_source_are_safe(self, monkeypatch, tmp_path):
        """源读取结果中同一 fingerprint 出现两次时（防御性），skip 策略下第二次不再重复写。"""
        entries = [_entry("fp-dup"), _entry("fp-dup")]
        source_cls = _make_source_class(entries)
        target_cls = _make_target_class()
        _install_stores(monkeypatch, source_cls, target_cls)

        report = f.migrate_knowledge_entries(
            source_backend="postgresql",
            target_path=str(tmp_path / "t.sqlite3"),
            on_conflict="upsert",
        )

        assert report["read"] == 2
        assert report["migrated"] == 2


# ── 6. 目标保护与备份（B9） ──────────────────────────────────────────


class TestBackupAndProtection:
    def test_existing_target_without_backup_is_refused(self, monkeypatch, tmp_path):
        """B9：目标已存在 + backup=False → 拒绝执行，目标文件原样保留。"""
        db = tmp_path / "t.sqlite3"
        SQLiteKnowledgeBaseStore(db_path=str(db)).upsert_kb_entry(
            _entry("fp-a", fix_suggestion="既有经验")
        )
        before = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()

        with pytest.raises(ValueError, match="backup"):
            _migrate(monkeypatch, [_entry("fp-b")], db, backup=False)

        after = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()
        assert after == before, "拒绝执行后目标文件必须原样保留"

    def test_backup_captures_pre_migration_content(self, monkeypatch, tmp_path):
        """B9：目标已存在 + backup=True → 先生成带时间戳备份，且备份内容 = 迁移前状态。"""
        db = tmp_path / "t.sqlite3"
        SQLiteKnowledgeBaseStore(db_path=str(db)).upsert_kb_entry(
            _entry("fp-a", fix_suggestion="迁移前")
        )

        report = _migrate(monkeypatch, [_entry("fp-b")], db, backup=True)

        backup_path = report["backup_path"]
        assert backup_path is not None
        assert Path(backup_path).exists()
        assert backup_path != str(db)
        backup_rows = SQLiteKnowledgeBaseStore(db_path=backup_path).list_recent_kb_entries()
        assert [r["fingerprint"] for r in backup_rows] == ["fp-a"]
        assert backup_rows[0]["fix_suggestion"] == "迁移前"
        # 主库继续正常迁移
        rows = SQLiteKnowledgeBaseStore(db_path=str(db)).list_recent_kb_entries()
        assert sorted(r["fingerprint"] for r in rows) == ["fp-a", "fp-b"]

    def test_no_backup_when_target_absent(self, monkeypatch, tmp_path):
        """目标不存在时（首次迁移）不产生备份，report backup_path=None。"""
        db = tmp_path / "fresh.sqlite3"
        report = _migrate(monkeypatch, [_entry("fp-1")], db, backup=True)

        assert report["backup_path"] is None
        assert db.exists()
        backups = [p for p in tmp_path.iterdir() if p != db]
        assert backups == []

    def test_target_path_memory_is_rejected(self, monkeypatch):
        """§4.2：target_path 拒绝 ':memory:'（SQLite 短连接下内存库无法承载持久化）。"""
        with pytest.raises(ValueError, match=":memory:"):
            _migrate(monkeypatch, [_entry("fp-1")], ":memory:")

    def test_target_path_empty_is_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="target_path"):
            _migrate(monkeypatch, [_entry("fp-1")], "   ")


# ── 7. 失败模式（B8） ────────────────────────────────────────────────


class TestFailureModes:
    def _entries(self):
        return [_entry("fp-1"), _entry("fp-2"), _entry("fp-3")]

    def test_fail_fast_stops_at_first_error(self, monkeypatch, tmp_path):
        """B8：fail_fast=True 首错即停，保留已完成写入，后续行不再尝试。"""
        target_cls = _make_target_class(fail_on={"fp-2"})
        _install_stores(monkeypatch, _make_source_class(self._entries()), target_cls)

        report = f.migrate_knowledge_entries(
            source_backend="postgresql",
            target_path=str(tmp_path / "t.sqlite3"),
            fail_fast=True,
        )

        assert report["status"] == "failed"
        assert report["migrated"] == 1
        assert report["read"] == 3
        assert len(report["errors"]) == 1
        assert report["errors"][0]["fingerprint"] == "fp-2"
        assert report["errors"][0]["error"]
        # fp-3 未被尝试：upsert 调用次数 = 2（fp-1 成功 + fp-2 失败）
        assert target_cls.method_calls.count("upsert_kb_entry") == 2

    def test_no_fail_fast_isolates_row_errors(self, monkeypatch, tmp_path):
        """B8：fail_fast=False 逐行隔离错误并全部计入 report，禁止静默宣称完整。"""
        target_cls = _make_target_class(fail_on={"fp-2", "fp-3"})
        _install_stores(monkeypatch, _make_source_class(self._entries()), target_cls)

        report = f.migrate_knowledge_entries(
            source_backend="postgresql",
            target_path=str(tmp_path / "t.sqlite3"),
            fail_fast=False,
        )

        assert report["status"] == "failed"
        assert report["migrated"] == 1
        assert len(report["errors"]) == 2
        assert [e["fingerprint"] for e in report["errors"]] == ["fp-2", "fp-3"]
        assert target_cls.method_calls.count("upsert_kb_entry") == 3, "所有行都被尝试过"

    def test_missing_fingerprint_is_a_row_error(self, monkeypatch, tmp_path):
        """缺 fingerprint 的行无法 upsert → 计入 errors（fail_fast=False 时不中断）。"""
        entries = [_entry("fp-ok"), {"analysis": {}, "updated_at": 1.0}]
        target_cls = _make_target_class()
        _install_stores(monkeypatch, _make_source_class(entries), target_cls)

        report = f.migrate_knowledge_entries(
            source_backend="postgresql", target_path=str(tmp_path / "t.sqlite3")
        )

        assert report["status"] == "failed"
        assert report["migrated"] == 1
        assert len(report["errors"]) == 1
        assert report["errors"][0]["fingerprint"] is None

    def test_row_errors_return_report_instead_of_raising(self, monkeypatch, tmp_path):
        """行级失败经 report 暴露（B8），不由 migrate 入口抛异常（异常留给基础设施级错误）。"""
        _install_stores(
            monkeypatch, _make_source_class(self._entries()), _make_target_class(fail_on={"fp-1"})
        )
        report = f.migrate_knowledge_entries(
            source_backend="postgresql", target_path=str(tmp_path / "t.sqlite3")
        )
        assert report["status"] == "failed"


# ── 8. checkpoint（sqlite_kb_store 新增面） ──────────────────────────


class TestCheckpoint:
    def test_checkpoint_is_not_abstract_and_available(self):
        assert hasattr(SQLiteKnowledgeBaseStore, "checkpoint")
        assert "checkpoint" not in KnowledgeBaseStorage.__abstractmethods__

    def test_checkpoint_makes_main_file_self_contained(self, tmp_path):
        """WAL 收口：checkpoint 后仅复制主文件即可获得全部数据（备份完整性前提）。"""
        db = tmp_path / "t.sqlite3"
        store = SQLiteKnowledgeBaseStore(db_path=str(db))
        store.upsert_kb_entry(_entry("fp-1"))
        store.checkpoint()

        copy_path = tmp_path / "copy.sqlite3"
        shutil.copy2(db, copy_path)

        rows = SQLiteKnowledgeBaseStore(db_path=str(copy_path)).list_recent_kb_entries()
        assert [r["fingerprint"] for r in rows] == ["fp-1"]

    def test_migration_checkpoints_target_after_writes(self, monkeypatch, tmp_path):
        """真实迁移路径结束时对目标执行 checkpoint（§4.2 第 8 步）。"""
        db = tmp_path / "t.sqlite3"
        store = SQLiteKnowledgeBaseStore(db_path=str(db))
        calls = {"checkpoint": 0}
        real_checkpoint = store.checkpoint

        def counted_checkpoint() -> None:
            calls["checkpoint"] += 1
            real_checkpoint()

        store.checkpoint = counted_checkpoint  # 实例属性遮蔽方法，仅用于计数
        monkeypatch.setattr(
            "app.runtime.core.storage.sqlite_kb_store.SQLiteKnowledgeBaseStore",
            lambda db_path=None: store,
        )
        _install_source(monkeypatch, _make_source_class([_entry("fp-1")]))

        f.migrate_knowledge_entries(source_backend="postgresql", target_path=str(db))
        assert calls["checkpoint"] >= 1

    def test_dry_run_does_not_checkpoint_existing_target(self, monkeypatch, tmp_path):
        """dry_run 对已存在的目标只读：不 checkpoint、不写入。"""
        db = tmp_path / "t.sqlite3"
        store = SQLiteKnowledgeBaseStore(db_path=str(db))
        store.upsert_kb_entry(_entry("fp-a"))
        calls = {"checkpoint": 0}
        real_checkpoint = store.checkpoint

        def counted_checkpoint() -> None:
            calls["checkpoint"] += 1
            real_checkpoint()

        store.checkpoint = counted_checkpoint  # 实例属性遮蔽方法，仅用于计数
        monkeypatch.setattr(
            "app.runtime.core.storage.sqlite_kb_store.SQLiteKnowledgeBaseStore",
            lambda db_path=None: store,
        )
        _install_source(monkeypatch, _make_source_class([_entry("fp-b")]))

        report = f.migrate_knowledge_entries(
            source_backend="postgresql", target_path=str(db), dry_run=True
        )

        assert report["dry_run"] is True
        assert calls["checkpoint"] == 0


# ── 9. CLI：零数据库代码 + 参数映射 + 退出码 ─────────────────────────


def _load_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("migrate_pg_kb_to_sqlite", CLI_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCli:
    def test_cli_exists(self):
        assert CLI_SCRIPT.exists()

    def test_cli_imports_only_stdlib_and_factory_entry(self):
        """AST 级证明：CLI 只允许 import 标准库参数/输出模块与 factory 迁移入口。"""
        tree = ast.parse(CLI_SCRIPT.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        allowed = {"argparse", "json", "sys", "__future__", "app", "pathlib"}
        unexpected = {name for name in imported if name.split(".")[0] not in allowed}
        assert unexpected == set(), f"CLI 出现非法 import：{unexpected}"
        factory_imports = {
            name for name in imported if name.startswith("app.runtime.core.storage.factory")
        }
        assert factory_imports, "CLI 必须经 factory.migrate_knowledge_entries 调用迁移"

    def test_cli_contains_no_sql_or_driver_tokens(self):
        """静态扫描：零 SQL / 零驱动 / 零 schema 知识。"""
        text = CLI_SCRIPT.read_text(encoding="utf-8")
        for token in (
            "psycopg2",
            "asyncpg",
            "sqlite3",
            "SELECT ",
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "CREATE TABLE",
            "PRAGMA",
            ".execute(",
            "execute_sql",
            ".connect(",
        ):
            assert token not in text, f"CLI 不得包含数据库代码令牌：{token}"

    def test_cli_maps_arguments_and_returns_zero(self, monkeypatch, capsys):
        cli = _load_cli()
        captured = {}

        def fake_migrate(**kwargs):
            captured.update(kwargs)
            return {"status": "ok", "read": 3, "migrated": 3, "skipped_existing": 0}

        monkeypatch.setattr(
            "app.runtime.core.storage.factory.migrate_knowledge_entries", fake_migrate
        )
        code = cli.main(
            [
                "--target-path",
                "out/kb.sqlite3",
                "--limit",
                "7",
                "--on-conflict",
                "upsert",
                "--dry-run",
                "--fail-fast",
                "--no-backup",
            ]
        )

        assert code == 0
        assert captured == {
            "source_backend": "postgresql",
            "target_path": "out/kb.sqlite3",
            "limit": 7,
            "on_conflict": "upsert",
            "dry_run": True,
            "fail_fast": True,
            "backup": False,
        }
        out = capsys.readouterr().out
        assert '"status": "ok"' in out, "CLI 应把 report 打印到 stdout"

    def test_cli_failed_report_returns_nonzero(self, monkeypatch):
        cli = _load_cli()
        monkeypatch.setattr(
            "app.runtime.core.storage.factory.migrate_knowledge_entries",
            lambda **kwargs: {"status": "failed", "errors": [{"fingerprint": "x"}]},
        )
        assert cli.main(["--target-path", "t.sqlite3"]) == 1

    def test_cli_does_not_swallow_exceptions(self, monkeypatch):
        cli = _load_cli()

        def boom(**kwargs):
            raise RuntimeError("pg down")

        monkeypatch.setattr(
            "app.runtime.core.storage.factory.migrate_knowledge_entries", boom
        )
        with pytest.raises(RuntimeError, match="pg down"):
            cli.main(["--target-path", "t.sqlite3"])

    def test_cli_rejects_non_postgresql_source(self, monkeypatch):
        cli = _load_cli()
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--target-path", "t.sqlite3", "--source-backend", "memory"])
        assert exc_info.value.code == 2, "argparse choices 应在参数层拒绝非法源"

    def test_cli_prints_no_connection_strings(self, monkeypatch, capsys):
        """report 打印路径上无凭据（B10 的 CLI 侧验证）。"""
        cli = _load_cli()
        monkeypatch.setattr(
            "app.runtime.core.storage.factory.migrate_knowledge_entries",
            lambda **kwargs: {"status": "ok", "read": 0, "migrated": 0},
        )
        cli.main(["--target-path", "t.sqlite3"])
        captured = capsys.readouterr()
        for marker in ("password", "postgres://", "postgresql://"):
            assert marker not in captured.out
            assert marker not in captured.err

    def test_cli_bootstrap_works_on_direct_file_execution(self, tmp_path):
        """WP2 修复回归：按「文件路径直接执行」时脚本必须自行完成项目根引导。

        子进程模拟真实缺陷场景：PYTHONPATH 移除、cwd 不在项目根（证明引导
        只依赖脚本自身位置，不依赖当前工作目录）。先证明引导前 app 不可导入
        （基线成立），再经 runpy 执行脚本顶层（不进入 main()，零数据库访问），
        断言 app 可导入且迁移入口可用。不连接真实 PG、不新增 skip。
        """
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        code = (
            "import sys\n"
            "try:\n"
            "    import app  # noqa: F401\n"
            "except ModuleNotFoundError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('基线不成立：引导前 app 不应可导入')\n"
            "import runpy\n"
            f"runpy.run_path({str(CLI_SCRIPT)!r}, run_name='wp2_bootstrap_check')\n"
            "import app.runtime.core.storage.factory as f\n"
            "assert callable(f.migrate_knowledge_entries)\n"
            "print('BOOTSTRAP_OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        assert "BOOTSTRAP_OK" in proc.stdout

"""M1-B: 经验生命周期测试。

覆盖三个核心缺口：
1. source 优先级保护——LLM upsert 不覆盖 seed/user_confirmed 的 analysis/fix_suggestion
2. verify 工具验证成功时写回 KB，验证失败时不写回
3. diagnose_issue 返回 related_experiences 字段

测试隔离：使用独立 KnowledgeBaseStore 实例，不污染全局单例。
"""
from __future__ import annotations

import pytest

from app.rag.knowledge_base import KnowledgeBaseStore


# ---------------------------------------------------------------------------
# 1. source 优先级保护
# ---------------------------------------------------------------------------


class TestSourcePriority:
    """LLM upsert 不得覆盖 seed / user_confirmed 的 analysis 和 fix_suggestion。"""

    def test_llm_does_not_overwrite_seed_analysis(self):
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "bad input", "root_cause": "seed cause"},
            fix_suggestion="seed fix",
            source="seed",
        )
        # LLM 分析结果尝试覆盖同指纹条目
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "bad input", "root_cause": "llm cause"},
            fix_suggestion="llm fix",
            source="llm",
        )
        entry = store.get("fp-1")
        assert entry is not None
        # seed 的 analysis 和 fix_suggestion 应保留
        assert entry["analysis"]["root_cause"] == "seed cause"
        assert entry["fix_suggestion"] == "seed fix"
        assert entry["source"] == "seed"

    def test_llm_does_not_overwrite_debug_case_seed_analysis(self):
        """真实内置 DebugCase 使用 debug_case source，必须同样受保护。"""
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-debug-case",
            analysis={"exception_type": "ValueError", "message": "bad input", "root_cause": "built-in cause"},
            fix_suggestion="built-in fix",
            source="debug_case",
        )
        store.upsert(
            fingerprint="fp-debug-case",
            analysis={"exception_type": "ValueError", "message": "bad input", "root_cause": "llm cause"},
            fix_suggestion="llm fix",
            source="llm",
        )

        entry = store.get("fp-debug-case")
        assert entry is not None
        assert entry["analysis"]["root_cause"] == "built-in cause"
        assert entry["fix_suggestion"] == "built-in fix"
        assert entry["source"] == "debug_case"

    def test_llm_preserves_seed_verify_count_and_confidence(self):
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "KeyError", "message": "missing"},
            fix_suggestion="seed fix",
            source="seed",
            verify_count=3,
            case_confidence=0.9,
        )
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "KeyError", "message": "missing", "root_cause": "llm"},
            fix_suggestion="llm fix",
            source="llm",
        )
        entry = store.get("fp-1")
        assert entry is not None
        assert entry["verify_count"] == 3
        assert entry["case_confidence"] == 0.9

    def test_user_confirmed_overwrites_seed(self):
        """user_confirmed 优先级高于 seed，可以覆盖。"""
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "msg", "root_cause": "seed"},
            fix_suggestion="seed fix",
            source="seed",
        )
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "msg", "root_cause": "confirmed"},
            fix_suggestion="confirmed fix",
            source="user_confirmed",
        )
        entry = store.get("fp-1")
        assert entry is not None
        assert entry["analysis"]["root_cause"] == "confirmed"
        assert entry["fix_suggestion"] == "confirmed fix"
        assert entry["source"] == "user_confirmed"

    def test_llm_overwrites_llm(self):
        """同优先级 source 之间允许覆盖。"""
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "msg", "root_cause": "llm-1"},
            fix_suggestion="fix-1",
            source="llm",
        )
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "msg", "root_cause": "llm-2"},
            fix_suggestion="fix-2",
            source="llm",
        )
        entry = store.get("fp-1")
        assert entry is not None
        assert entry["analysis"]["root_cause"] == "llm-2"
        assert entry["fix_suggestion"] == "fix-2"

    def test_llm_does_not_overwrite_user_confirmed(self):
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "msg", "root_cause": "confirmed"},
            fix_suggestion="confirmed fix",
            source="user_confirmed",
            case_confidence=0.95,
            verify_count=2,
        )
        store.upsert(
            fingerprint="fp-1",
            analysis={"exception_type": "ValueError", "message": "msg", "root_cause": "llm"},
            fix_suggestion="llm fix",
            source="llm",
        )
        entry = store.get("fp-1")
        assert entry is not None
        assert entry["analysis"]["root_cause"] == "confirmed"
        assert entry["fix_suggestion"] == "confirmed fix"
        assert entry["source"] == "user_confirmed"
        assert entry["case_confidence"] == 0.95
        assert entry["verify_count"] == 2

    def test_explicit_lifecycle_stats_are_monotonic(self):
        """显式 upsert 统计不能降低已有验证证据。"""
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-monotonic",
            analysis={"exception_type": "ValueError", "message": "msg"},
            fix_suggestion="fix",
            source="user_confirmed",
            verify_count=4,
            case_confidence=0.9,
        )
        entry = store.upsert(
            fingerprint="fp-monotonic",
            analysis={"exception_type": "ValueError", "message": "msg"},
            fix_suggestion="updated fix",
            source="user_confirmed",
            verify_count=0,
            case_confidence=0.1,
        )

        assert entry["verify_count"] == 4
        assert entry["case_confidence"] == 0.9

    def test_explicit_lifecycle_stats_can_increase(self):
        """显式 upsert 允许补充更高的验证统计。"""
        store = KnowledgeBaseStore(max_entries=10)
        store.upsert(
            fingerprint="fp-increase",
            analysis={"exception_type": "ValueError", "message": "msg"},
            fix_suggestion="fix",
            source="llm",
            verify_count=1,
            case_confidence=0.2,
        )
        entry = store.upsert(
            fingerprint="fp-increase",
            analysis={"exception_type": "ValueError", "message": "msg"},
            fix_suggestion="updated fix",
            source="llm",
            verify_count=3,
            case_confidence=0.8,
        )

        assert entry["verify_count"] == 3
        assert entry["case_confidence"] == 0.8


# ---------------------------------------------------------------------------
# 2. verify 工具验证成功写回 KB / 验证失败不写回
# ---------------------------------------------------------------------------


class TestVerifyKbWriteback:
    """verify 工具验证成功时递增 KB verify_count；失败时不写回。"""

    def test_verify_success_increments_verify_count(self, monkeypatch):
        from app.mcp.tools.verify_api import verify_handler

        # 在 KB 中预置一条经验
        from app.rag.knowledge_base import get_knowledge_base
        kb = get_knowledge_base()
        kb.clear()
        kb.upsert(
            fingerprint="fp-verify-test",
            analysis={"exception_type": "ValueError", "message": "test error"},
            fix_suggestion="test fix",
            source="seed",
        )
        assert kb.get("fp-verify-test")["verify_count"] == 0

        # mock build_debug_context 返回带指纹的上下文（patch 源头模块）
        from app.schemas import DebugContext
        mock_ctx = DebugContext(
            request_id="test-trace",
            exception={"type": "ValueError", "message": "test error", "fingerprint": "fp-verify-test"},
        )
        monkeypatch.setattr(
            "app.runtime.context.builder.build_debug_context",
            lambda tid, **kw: mock_ctx,
        )

        result = verify_handler({
            "actual": {"status_code": 200, "body": {"ok": True}},
            "spec": {"kind": "api", "target": "test", "expect": {"status": 200, "body_rules": {"ok": True}}},
            "trace_id": "test-trace",
        })

        assert result["matched"] is True
        entry = kb.get("fp-verify-test")
        assert entry["verify_count"] == 1
        assert entry["case_confidence"] >= 0.7

        # 清理
        kb.clear()

    def test_verify_failure_does_not_writeback(self, monkeypatch):
        from app.mcp.tools.verify_api import verify_handler
        from app.rag.knowledge_base import get_knowledge_base

        kb = get_knowledge_base()
        kb.clear()
        kb.upsert(
            fingerprint="fp-verify-fail",
            analysis={"exception_type": "ValueError", "message": "test"},
            fix_suggestion="fix",
            source="seed",
        )
        assert kb.get("fp-verify-fail")["verify_count"] == 0

        from app.schemas import DebugContext
        mock_ctx = DebugContext(
            request_id="test-trace-fail",
            exception={"type": "ValueError", "message": "test", "fingerprint": "fp-verify-fail"},
        )
        monkeypatch.setattr(
            "app.runtime.context.builder.build_debug_context",
            lambda tid, **kw: mock_ctx,
        )

        result = verify_handler({
            "actual": {"status_code": 200, "body": {"ok": False}},
            "spec": {"kind": "api", "target": "test", "expect": {"status": 200, "body_rules": {"ok": True}}},
            "trace_id": "test-trace-fail",
        })

        assert result["matched"] is False
        entry = kb.get("fp-verify-fail")
        assert entry["verify_count"] == 0  # 验证失败不写回

        kb.clear()

    def test_verify_no_trace_id_no_writeback(self, monkeypatch):
        from app.mcp.tools.verify_api import verify_handler
        from app.rag.knowledge_base import get_knowledge_base

        kb = get_knowledge_base()
        kb.clear()
        kb.upsert(
            fingerprint="fp-no-trace",
            analysis={"exception_type": "ValueError", "message": "test"},
            fix_suggestion="fix",
            source="seed",
        )

        result = verify_handler({
            "actual": {"status_code": 200, "body": {"ok": True}},
            "spec": {"kind": "api", "target": "test", "expect": {"status": 200, "body_rules": {"ok": True}}},
            # 无 trace_id
        })

        assert result["matched"] is True
        entry = kb.get("fp-no-trace")
        assert entry["verify_count"] == 0  # 无 trace_id 不写回

        kb.clear()

    def test_verify_kb_miss_silently_skips(self, monkeypatch):
        """verify 成功但 fingerprint 在 KB 中不存在时静默降级，不报错。"""
        from app.mcp.tools.verify_api import verify_handler
        from app.rag.knowledge_base import get_knowledge_base

        kb = get_knowledge_base()
        kb.clear()
        # KB 中没有对应 fingerprint 的条目

        from app.schemas import DebugContext
        mock_ctx = DebugContext(
            request_id="test-trace-miss",
            exception={"type": "ValueError", "message": "test", "fingerprint": "fp-not-in-kb"},
        )
        monkeypatch.setattr(
            "app.runtime.context.builder.build_debug_context",
            lambda tid, **kw: mock_ctx,
        )

        result = verify_handler({
            "actual": {"status_code": 200, "body": {"ok": True}},
            "spec": {"kind": "api", "target": "test", "expect": {"status": 200, "body_rules": {"ok": True}}},
            "trace_id": "test-trace-miss",
        })

        # verify 结论不受影响
        assert result["matched"] is True
        # KB 中确实没有该条目
        assert kb.get("fp-not-in-kb") is None

        kb.clear()

    def test_verify_writeback_failure_does_not_change_result(self, monkeypatch):
        """verify 写回 KB 时如果 record_verification 抛异常，verify 结论不变。"""
        from app.mcp.tools.verify_api import verify_handler
        from app.rag.knowledge_base import get_knowledge_base

        kb = get_knowledge_base()
        kb.clear()
        kb.upsert(
            fingerprint="fp-writeback-fail",
            analysis={"exception_type": "ValueError", "message": "test"},
            fix_suggestion="fix",
            source="seed",
        )

        from app.schemas import DebugContext
        mock_ctx = DebugContext(
            request_id="test-trace-wb-fail",
            exception={"type": "ValueError", "message": "test", "fingerprint": "fp-writeback-fail"},
        )
        monkeypatch.setattr(
            "app.runtime.context.builder.build_debug_context",
            lambda tid, **kw: mock_ctx,
        )
        # mock record_verification 抛异常
        import app.rag.knowledge_base as kb_module
        monkeypatch.setattr(
            kb_module,
            "record_verification",
            lambda fp, confidence: (_ for _ in ()).throw(RuntimeError("simulated")),
        )

        result = verify_handler({
            "actual": {"status_code": 200, "body": {"ok": True}},
            "spec": {"kind": "api", "target": "test", "expect": {"status": 200, "body_rules": {"ok": True}}},
            "trace_id": "test-trace-wb-fail",
        })

        # verify 结论不受写回失败影响
        assert result["matched"] is True

        kb.clear()


# ---------------------------------------------------------------------------
# 3. diagnose_issue 返回 related_experiences
# ---------------------------------------------------------------------------


class TestDiagnoseRelatedExperiences:
    """diagnose_issue 应返回 related_experiences 字段。"""

    def test_returns_related_experiences_on_found(self, monkeypatch):
        from app.mcp.tools.diagnose_api import handler
        from app.rag.knowledge_base import get_knowledge_base

        kb = get_knowledge_base()
        kb.clear()
        kb.upsert(
            fingerprint="fp-diag-test",
            analysis={"exception_type": "ValueError", "message": "bad input"},
            fix_suggestion="validate input",
            source="seed",
        )

        # mock errors.get_latest 返回一条错误
        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api.errors.get_latest",
            lambda session_id=None: {"error_id": "err-1", "type": "ValueError", "message": "bad input", "fingerprint": "fp-diag-test"},
        )

        # mock _build_context 返回带指纹的上下文
        mock_ctx = {
            "request_id": "err-1",
            "exception": {"type": "ValueError", "message": "bad input", "fingerprint": "fp-diag-test"},
        }
        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api._build_context",
            lambda tid, session_id=None: mock_ctx,
        )

        result = handler({})

        assert result["found"] is True
        assert "related_experiences" in result
        assert len(result["related_experiences"]) == 1
        exp = result["related_experiences"][0]
        assert exp["fingerprint"] == "fp-diag-test"
        assert exp["fix_suggestion"] == "validate input"
        assert exp["source"] == "seed"

        kb.clear()

    def test_related_experiences_empty_on_no_kb_hit(self, monkeypatch):
        from app.mcp.tools.diagnose_api import handler
        from app.rag.knowledge_base import get_knowledge_base

        kb = get_knowledge_base()
        kb.clear()

        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api.errors.get_latest",
            lambda session_id=None: {"error_id": "err-2", "type": "RuntimeError", "message": "unknown", "fingerprint": "fp-unknown"},
        )

        mock_ctx = {
            "request_id": "err-2",
            "exception": {"type": "RuntimeError", "message": "unknown", "fingerprint": "fp-unknown"},
        }
        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api._build_context",
            lambda tid, session_id=None: mock_ctx,
        )

        result = handler({})

        assert result["found"] is True
        assert "related_experiences" in result
        assert result["related_experiences"] == []

    def test_related_experiences_empty_on_not_found(self, monkeypatch):
        from app.mcp.tools.diagnose_api import handler

        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api.errors.get_latest",
            lambda session_id=None: None,
        )
        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api.errors.get_by_id",
            lambda tid, session_id=None: None,
        )
        # mock list_recent_traces 返回空
        import app.mcp.tools.trace_api as trace_api
        monkeypatch.setattr(trace_api, "list_recent_traces", lambda **kw: [])

        result = handler({})

        assert result["found"] is False
        # not_found 不含 related_experiences（无诊断上下文）

    def test_request_id_path_includes_experiences(self, monkeypatch):
        from app.mcp.tools.diagnose_api import handler
        from app.rag.knowledge_base import get_knowledge_base

        kb = get_knowledge_base()
        kb.clear()
        kb.upsert(
            fingerprint="fp-req-id",
            analysis={"exception_type": "KeyError", "message": "'missing_key'"},
            fix_suggestion="use .get()",
            source="seed",
        )

        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api.errors.get_by_id",
            lambda tid, session_id=None: {"error_id": tid, "type": "KeyError", "message": "'missing_key'"},
        )

        mock_ctx = {
            "request_id": "req-1",
            "exception": {"type": "KeyError", "message": "'missing_key'", "fingerprint": "fp-req-id"},
        }
        monkeypatch.setattr(
            "app.mcp.tools.diagnose_api._build_context",
            lambda tid, session_id=None: mock_ctx,
        )

        result = handler({"request_id": "req-1"})

        assert result["found"] is True
        assert "related_experiences" in result
        assert len(result["related_experiences"]) == 1
        assert result["related_experiences"][0]["fingerprint"] == "fp-req-id"

        kb.clear()


# ---------------------------------------------------------------------------
# 4. SQLite 兼容性——不改变 schema，数据兼容
# ---------------------------------------------------------------------------


class TestSqliteCompat:
    """确保 M1-B 不改变 SQLite schema，现有数据兼容。"""

    def test_sqlite_schema_unchanged(self, tmp_path):
        """SQLite 表结构应与 v0.9.1 一致（无新列）。"""
        from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore

        db_path = str(tmp_path / "compat_test.sqlite3")
        store = SQLiteKnowledgeBaseStore(db_path=db_path)
        store.upsert_kb_entry({
            "fingerprint": "fp-compat",
            "analysis": {"exception_type": "ValueError", "message": "test"},
            "fix_suggestion": "fix",
            "source": "seed",
            "created_at": 1000.0,
            "updated_at": 1000.0,
            "normalized_fingerprint": "norm",
            "type_fingerprint": "type",
            "verify_count": 0,
            "case_confidence": 0.8,
        })

        # 用新实例回灌，验证 schema 兼容
        store2 = SQLiteKnowledgeBaseStore(db_path=db_path)
        rows = store2.list_recent_kb_entries(limit=10)
        assert len(rows) == 1
        assert rows[0]["fingerprint"] == "fp-compat"
        assert rows[0]["source"] == "seed"
        assert rows[0]["verify_count"] == 0
        assert rows[0]["case_confidence"] == 0.8

    def test_sqlite_write_through_preserves_source_priority(self, tmp_path, monkeypatch):
        """写穿到 SQLite 后回灌，source 优先级保护仍然生效。"""
        import app.rag.knowledge_base as kb_module
        from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore

        db_path = str(tmp_path / "priority_test.sqlite3")
        sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)
        monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: sqlite_store)

        store = KnowledgeBaseStore(max_entries=10)
        # 先写 seed
        store.upsert(
            fingerprint="fp-priority",
            analysis={"exception_type": "ValueError", "message": "test", "root_cause": "seed"},
            fix_suggestion="seed fix",
            source="seed",
        )
        # 再写 llm（不应覆盖 seed）
        store.upsert(
            fingerprint="fp-priority",
            analysis={"exception_type": "ValueError", "message": "test", "root_cause": "llm"},
            fix_suggestion="llm fix",
            source="llm",
        )

        # 模拟重启：新实例回灌
        store2 = KnowledgeBaseStore(max_entries=10)
        loaded = store2.load_from_persistent()
        assert loaded == 1

        entry = store2.get("fp-priority")
        assert entry is not None
        # seed 的内容应保留
        assert entry["source"] == "seed"
        assert entry["fix_suggestion"] == "seed fix"
        assert entry["analysis"]["root_cause"] == "seed"

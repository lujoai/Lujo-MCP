"""M1-C: KB 经验生命周期真实链路集成测试。

把 M1-B 从「单元测试通过」提升到「真实链路集成验证」：

    真实 trace → diagnose_issue → related_experiences → verify 成功
    → KB writeback → verify_count/confidence 更新 → SQLite 持久化
    → 重新加载后仍可检索。

与 tests/unit/test_m1b_experience_lifecycle.py 的区别：
- 单测用 monkeypatch mock 掉 build_debug_context / errors 缓冲；
- 本文件走真实生产入口（save_trace / diagnose_api.handler /
  verify_api.verify_handler）与真实 SQLite 写穿，只在测试隔离层把 KB 持久化
  重定向到 pytest tmp_path 下的临时 SQLite（不触碰项目根 lujo-kb.sqlite3）。

不连接 PostgreSQL / Redis / Docker / 外部 LLM，不读 .env。
"""
from __future__ import annotations

import pytest

from app.mcp.tools.diagnose_api import handler as diagnose_handler
from app.mcp.tools.verify_api import verify_handler
from app.rag import knowledge_base as kb_module
from app.rag.debug_case import (
    compute_normalized_fingerprint,
    compute_type_fingerprint,
)
from app.runtime.core import errors
from app.runtime.core.storage.sqlite_kb_store import SQLiteKnowledgeBaseStore
from app.runtime.core.trace_repo import save_trace

# 固定堆栈帧：fingerprint 由 compute_fingerprint("ValueError", FRAMES) 确定性计算，
# save_trace 内部与之同一算法同一帧，保证真实 trace 与 KB 条目指纹对齐。
FRAMES = [{"file": "app/service/transfer.py", "function": "transfer", "line": 42}]
EXC_TYPE = "ValueError"
MESSAGE = "balance is -10"
FINGERPRINT = errors.compute_fingerprint(EXC_TYPE, FRAMES)

VALID_SPEC = {
    "kind": "api",
    "target": "POST /transfer",
    "expect": {"status": 200, "body_rules": {"ok": True}},
}
MATCHING_ACTUAL = {"status_code": 200, "body": {"ok": True}}
MISMATCHING_ACTUAL = {"status_code": 200, "body": {"ok": False}}


@pytest.fixture
def kb_sqlite(tmp_path, monkeypatch):
    """把 KB 持久化重定向到临时 SQLite，并清空全局 KB 单例，实现测试隔离。"""
    db_path = str(tmp_path / "m1c_kb.sqlite3")
    sqlite_store = SQLiteKnowledgeBaseStore(db_path=db_path)
    monkeypatch.setattr(kb_module, "get_knowledge_store", lambda: sqlite_store)
    kb = kb_module.get_knowledge_base()
    kb.clear()
    return sqlite_store, kb


def _new_trace() -> str:
    """创建一条真实 trace（生产 save_trace 入口），返回 error_id。"""
    return save_trace(EXC_TYPE, MESSAGE, FRAMES, source="ingest")


def _seed_kb(kb) -> str:
    """写入一条 seed（debug_case 口径）经验，返回其 fingerprint。"""
    kb.upsert(
        fingerprint=FINGERPRINT,
        analysis={
            "exception_type": EXC_TYPE,
            "message": MESSAGE,
            "root_cause": "seed cause: negative balance not validated",
        },
        fix_suggestion="seed fix: validate balance before transfer",
        source="seed",
    )
    return FINGERPRINT


class TestFullLifecycleChain:
    """真实 trace → diagnose → verify 写回 → SQLite 持久化 → 重载检索。"""

    def test_end_to_end_chain(self, kb_sqlite, monkeypatch):
        sqlite_store, kb = kb_sqlite

        # 0. 真实 trace（生产 ingestion 入口），指纹与 KB 条目对齐
        error_id = _new_trace()
        assert error_id == errors.get_latest()["error_id"]

        enter = kb.get(FINGERPRINT)
        assert enter is None  # 尚未写入经验

        # 1. seed 经验
        _seed_kb(kb)

        # 2. 低优先级 llm upsert：不覆盖 seed 的 analysis/fix_suggestion/source
        kb.upsert(
            fingerprint=FINGERPRINT,
            analysis={"exception_type": EXC_TYPE, "message": MESSAGE, "root_cause": "llm cause"},
            fix_suggestion="llm fix",
            source="llm",
        )

        # 3. 真实 diagnose（默认取最近一次真实错误）
        diag = diagnose_handler({})
        assert diag["found"] is True
        assert diag["trace_id"] == error_id
        exps = diag["related_experiences"]
        assert len(exps) == 1
        assert exps[0]["fingerprint"] == FINGERPRINT
        assert exps[0]["fix_suggestion"] == "seed fix: validate balance before transfer"
        assert exps[0]["source"] == "seed"

        # 4. 真实 verify（matched=true）→ 写回 KB
        result = verify_handler({
            "actual": MATCHING_ACTUAL,
            "spec": VALID_SPEC,
            "trace_id": error_id,
        })
        assert result["matched"] is True

        # 5. verify_count 增加、confidence 不降低、内容未被 llm 污染
        entry = kb.get(FINGERPRINT)
        assert entry["verify_count"] == 1
        assert entry["case_confidence"] >= 0.7
        assert entry["fix_suggestion"] == "seed fix: validate balance before transfer"
        assert entry["source"] == "seed"

        # 6. SQLite 已持久化（直接读底层表）
        rows = sqlite_store.list_recent_kb_entries(limit=10)
        assert len(rows) == 1
        assert rows[0]["fingerprint"] == FINGERPRINT
        assert rows[0]["verify_count"] == 1

        # 7. 模拟重启：全新 KnowledgeBaseStore + 从持久层回灌
        store2 = kb_module.KnowledgeBaseStore(max_entries=100)
        loaded = store2.load_from_persistent()
        assert loaded == 1
        reloaded = store2.get(FINGERPRINT)
        assert reloaded is not None
        assert reloaded["verify_count"] == 1
        assert reloaded["case_confidence"] >= 0.7
        assert reloaded["source"] == "seed"
        assert reloaded["fix_suggestion"] == "seed fix: validate balance before transfer"

        # 8. 索引经回灌后重建，三级检索仍可命中
        assert store2.get_by_normalized_fingerprint(
            compute_normalized_fingerprint(EXC_TYPE, MESSAGE)
        ) is not None
        assert store2.get_by_type_fingerprint(
            compute_type_fingerprint(EXC_TYPE)
        ) != []

        # 9. 重启回灌后替换进程内 KB 单例，公共 diagnose_handler 仍可召回经验
        monkeypatch.setattr(kb_module, "_knowledge_base", store2)
        monkeypatch.setattr(kb_module, "get_knowledge_base", lambda: store2)
        diag2 = diagnose_handler({})
        assert diag2["found"] is True
        assert diag2["trace_id"] == error_id
        exps2 = diag2["related_experiences"]
        assert len(exps2) == 1
        assert exps2[0]["fingerprint"] == FINGERPRINT
        assert exps2[0]["source"] == "seed"
        assert exps2[0]["fix_suggestion"] == "seed fix: validate balance before transfer"
        assert exps2[0]["verify_count"] == 1
        assert exps2[0]["case_confidence"] >= 0.7
        assert exps2[0]["case_confidence"] >= exps[0]["case_confidence"]


class TestSourcePriorityAndIndex:
    """真实 SQLite 写穿下 source 优先级与索引保留，重载后仍正确。"""

    def test_priority_and_indexes_survive_reload(self, kb_sqlite):
        sqlite_store, kb = kb_sqlite

        _seed_kb(kb)
        kb.upsert(
            fingerprint=FINGERPRINT,
            analysis={"root_cause": "llm-only analysis without type/message"},
            fix_suggestion="llm fix",
            source="llm",
        )

        entry = kb.get(FINGERPRINT)
        assert entry["source"] == "seed"
        assert entry["fix_suggestion"] == "seed fix: validate balance before transfer"
        assert entry["normalized_fingerprint"] == compute_normalized_fingerprint(EXC_TYPE, MESSAGE)
        assert entry["type_fingerprint"] == compute_type_fingerprint(EXC_TYPE)

        store2 = kb_module.KnowledgeBaseStore(max_entries=100)
        store2.load_from_persistent()
        reloaded = store2.get(FINGERPRINT)
        assert reloaded["source"] == "seed"
        assert reloaded["normalized_fingerprint"] == compute_normalized_fingerprint(EXC_TYPE, MESSAGE)
        assert reloaded["type_fingerprint"] == compute_type_fingerprint(EXC_TYPE)


class TestVerifyFailurePaths:
    """真实链路下 verify 失败 / 无 trace / KB miss 不污染 KB。"""

    def test_matched_false_does_not_writeback(self, kb_sqlite):
        sqlite_store, kb = kb_sqlite
        error_id = _new_trace()
        _seed_kb(kb)
        assert kb.get(FINGERPRINT)["verify_count"] == 0

        result = verify_handler({
            "actual": MISMATCHING_ACTUAL,
            "spec": VALID_SPEC,
            "trace_id": error_id,
        })
        assert result["matched"] is False

        entry = kb.get(FINGERPRINT)
        assert entry is not None
        assert entry["verify_count"] == 0
        assert entry["case_confidence"] == 0.0

    def test_kb_miss_silently_skips(self, kb_sqlite):
        """verify 成功但 fingerprint 不在 KB：结论不变，KB 不被污染。"""
        sqlite_store, kb = kb_sqlite
        error_id = _new_trace()
        assert kb.get(FINGERPRINT) is None

        result = verify_handler({
            "actual": MATCHING_ACTUAL,
            "spec": VALID_SPEC,
            "trace_id": error_id,
        })
        assert result["matched"] is True
        assert kb.get(FINGERPRINT) is None
        assert kb.size() == 0

    def test_no_trace_id_no_writeback(self, kb_sqlite):
        sqlite_store, kb = kb_sqlite
        _seed_kb(kb)
        assert kb.get(FINGERPRINT)["verify_count"] == 0

        result = verify_handler({
            "actual": MATCHING_ACTUAL,
            "spec": VALID_SPEC,
        })
        assert result["matched"] is True

        assert kb.get(FINGERPRINT)["verify_count"] == 0

    def test_writeback_exception_does_not_change_result(self, kb_sqlite, monkeypatch):
        """verify 写回抛异常：结论不变，KB verify_count 不被误改。"""
        sqlite_store, kb = kb_sqlite
        error_id = _new_trace()
        _seed_kb(kb)

        def _boom(fp, confidence):
            raise RuntimeError("simulated writeback failure")

        monkeypatch.setattr(kb_module, "record_verification", _boom)

        result = verify_handler({
            "actual": MATCHING_ACTUAL,
            "spec": VALID_SPEC,
            "trace_id": error_id,
        })
        assert result["matched"] is True
        assert kb.get(FINGERPRINT)["verify_count"] == 0
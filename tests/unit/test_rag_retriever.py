"""Debug Experience Retriever 单测：三层检索 / 合并排序 / 降级（P1 D2）。"""
import pytest

from app.config import settings
from app.rag.knowledge_base import clear_knowledge_base, upsert_knowledge_entry
from app.rag.retriever import retrieve_debug_experience


@pytest.fixture(autouse=True)
def _clean_kb():
    clear_knowledge_base()
    yield
    clear_knowledge_base()


def _upsert(fp: str, exc_type: str, message: str, fix: str = "fix"):
    upsert_knowledge_entry(
        fingerprint=fp,
        analysis={
            "exception_type": exc_type,
            "message": message,
            "root_cause": f"rc-{fp}",
            "source_files": [f"{fp}.py"],
        },
        fix_suggestion=fix,
        source="test",
    )


# ── 1. fingerprint 命中 ──


def test_fingerprint_hit():
    _upsert("fp-1", "TypeError", "object of type 'NoneType' has no len()")
    recs = retrieve_debug_experience(
        exc_type="TypeError", message="object of type 'NoneType' has no len()",
        fingerprint="fp-1",
    )
    assert len(recs) == 1
    assert recs[0].fingerprint == "fp-1"
    assert recs[0].source == "fingerprint"
    assert recs[0].solution == "fix"


def test_fingerprint_miss_falls_through():
    """fingerprint 未命中时走后续层级，不阻塞。"""
    _upsert("fp-2", "KeyError", "'user_id' not found at 0x7f8a1b2c 1234")
    recs = retrieve_debug_experience(
        exc_type="KeyError", message="'admin' not found at 0xdeadbeef 5678",
        fingerprint="no-such-fp",
    )
    assert recs and recs[0].source == "message_similarity"


# ── 2. message normalize 命中 ──


def test_message_normalize_hit():
    _upsert("fp-2", "KeyError", "'user_id' not found at 0x7f8a1b2c 1234")
    recs = retrieve_debug_experience(
        exc_type="KeyError", message="'admin' not found at 0xdeadbeef 5678"
    )
    assert len(recs) == 1
    assert recs[0].fingerprint == "fp-2"
    assert recs[0].source == "message_similarity"
    # 归一化剥离了变量值，仅剩模式
    assert recs[0].message_pattern == "not found at"


# ── 3. 多个结果排序 ──


def test_multiple_results_sorted_by_score():
    _upsert("fp-a", "KeyError", "connection to database failed at 10.0.0.1:5432")
    _upsert("fp-b", "KeyError", "connection refused: no route to host 10.0.0.1 port 5432")
    _upsert("fp-c", "KeyError", "file not found on disk")
    recs = retrieve_debug_experience(
        exc_type="KeyError", message="connection to database failed at 10.0.0.1:5432",
        top_k=3,
    )
    # fp-c 与 query 无公共 token → score=0 → 不返回；fp-a 与 query 完全一致排首位
    assert [r.fingerprint for r in recs] == ["fp-a", "fp-b"]
    assert recs[0].source == "message_similarity"


# ── 4. top_k 限制 ──


def test_top_k_limit():
    # 归一化剥离数字后 4 条消息模式相同，均会被召回
    for i in range(4):
        _upsert(f"fp-{i}", "ValueError", f"bad value number {i} in payload field")
    recs = retrieve_debug_experience(
        exc_type="ValueError", message="bad value number 2 in payload field", top_k=2
    )
    assert len(recs) == 2


def test_top_k_zero_returns_empty():
    _upsert("fp-1", "TypeError", "boom")
    assert retrieve_debug_experience(exc_type="TypeError", message="boom", top_k=0) == []


# ── 5. vector 关闭 ──


def test_vector_disabled_does_not_call_store(monkeypatch):
    """vector_store_enabled=False（默认）时不触碰 vector store。"""
    def boom(*_a, **_k):
        raise AssertionError("vector store must not be called when disabled")

    monkeypatch.setattr("app.rag.retriever.get_vector_store", boom)
    _upsert("fp-1", "TypeError", "object of type 'NoneType' has no len()")
    recs = retrieve_debug_experience(fingerprint="fp-1")
    assert len(recs) == 1
    assert recs[0].source == "fingerprint"


# ── 6. vector 异常降级 ──


def test_vector_exception_degrades(monkeypatch):
    """vector store 抛异常时返回已有结果，不影响主流程。"""
    monkeypatch.setattr(settings, "vector_store_enabled", True)

    def boom(*_a, **_k):
        raise RuntimeError("vector store down")

    monkeypatch.setattr("app.rag.retriever.get_vector_store", boom)
    _upsert("fp-1", "TypeError", "object of type 'NoneType' has no len()")
    recs = retrieve_debug_experience(
        exc_type="TypeError", message="object of type 'NoneType' has no len()",
        fingerprint="fp-1",
    )
    assert len(recs) == 1
    assert recs[0].source == "fingerprint"


# ── 7. KB 为空 ──


def test_empty_kb_returns_empty():
    assert retrieve_debug_experience(
        exc_type="KeyError", message="anything", fingerprint="nope"
    ) == []


# ── 8. 异常输入降级 ──


def test_invalid_input_degrades():
    assert retrieve_debug_experience() == []
    assert retrieve_debug_experience(exc_type=None, message=None) == []
    assert retrieve_debug_experience(exc_type="E", message=None) == []

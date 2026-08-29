"""v0.7.0 工作包②：KB 学习闭环可观测性测试。

核心纪律（v0.6.9 教训）：埋点必须挂在真实生产路径上——主断言走
``analyzer.analyze`` 主入口（携带真实生产指纹命中 KB，短路 LLM），
断言 counter 在真实链路上增长；禁止只在测试里直接调 record_*。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.context_assembler import RepairContextAssembler
from app.agent.verify_loop import _writeback_kb
from app.config import settings
from app.llm.analyzer import analyze
from app.llm.kb_integration import _persist_analysis_to_knowledge_base
from app.observability import _render_prometheus, get_kb_metric_snapshot
from app.rag.knowledge_base import clear_knowledge_base, get_knowledge_entry, upsert_knowledge_entry
from app.runtime.context.builder import build_debug_context
from app.runtime.core import errors, trace_repo


@pytest.fixture(autouse=True)
def _isolate():
    errors._recent.clear()
    clear_knowledge_base()
    yield
    errors._recent.clear()
    clear_knowledge_base()


_FRAMES = [
    {"file": "app/services/orders.py", "line": 42, "function": "create_order"},
    {"file": "app/api/routes.py", "line": 10, "function": "handler"},
]


def _real_context() -> dict:
    """真实生产链路 context：save_trace → errors.record → build_debug_context。"""
    error_id = trace_repo.save_trace(
        "ValueError", "kb-observability-contract", _FRAMES, source="test"
    )
    context = build_debug_context(error_id).model_dump()
    assert context["exception"]["fingerprint"], "生产链路必须产出指纹（R7-P1-2）"
    return context


# ---------------------------------------------------------------------------
# 交付物一：真实路径 counter 增长证明
# ---------------------------------------------------------------------------


class TestRealPathCounters:
    def test_analyze_kb_hit_increments_l1_counter(self):
        """主集成断言：真实 analyzer.analyze 主入口携带真实生产指纹命中 KB，
        l1_fingerprint counter 增长且结果来自 knowledge_base（短路 LLM）。"""
        context = _real_context()
        fingerprint = context["exception"]["fingerprint"]
        upsert_knowledge_entry(
            fingerprint=fingerprint,
            analysis={"root_cause": "known-cause", "confidence": "high"},
            fix_suggestion="known-fix",
            source="llm",
        )

        before = get_kb_metric_snapshot()["hits_by_level"]
        result = analyze(context)  # 真实主入口：KB 命中即短路，不调 LLM
        after = get_kb_metric_snapshot()["hits_by_level"]

        assert result["knowledge_base_hit"] is True
        assert result["analysis_source"] == "knowledge_base"
        assert result["analysis"]["root_cause"] == "known-cause"
        assert after["l1_fingerprint"] == before["l1_fingerprint"] + 1, (
            "埋点未挂在真实链路上（counter 未增长）"
        )

    def test_analyze_kb_miss_increments_miss_counter(self):
        """无命中时真实主入口计入 miss（闭环空转的关键观测信号）。"""
        context = _real_context()
        # 不种 KB 条目；向量库默认关闭 → vector_rag 无结果
        before = get_kb_metric_snapshot()["hits_by_level"]
        try:
            analyze(context)  # miss 后真实链路进入 LLM 调用（无 key 时抛错，属真实行为）
        except Exception:
            pass  # miss 埋点在 LLM 调用之前已完成
        after = get_kb_metric_snapshot()["hits_by_level"]

        assert after["miss"] == before["miss"] + 1

    def test_analysis_writeback_counter_via_real_persist(self):
        """分析回写：真实 _persist_analysis_to_knowledge_base 成功/跳过计数。"""
        context = _real_context()
        fingerprint = context["exception"]["fingerprint"]

        before = get_kb_metric_snapshot()["writeback"]["analysis"]
        _persist_analysis_to_knowledge_base(
            fingerprint, {"analysis": {"root_cause": "llm-says"}}, context
        )
        mid = get_kb_metric_snapshot()["writeback"]["analysis"]
        _persist_analysis_to_knowledge_base(None, {"analysis": {}}, context)
        after = get_kb_metric_snapshot()["writeback"]["analysis"]

        assert mid["success"] == before["success"] + 1
        assert after["skipped"] == before["skipped"] + 1
        # 真实写回生效
        assert get_knowledge_entry(fingerprint)["analysis"]["root_cause"] == "llm-says"

    def test_verify_writeback_counter_via_real_writeback(self, monkeypatch):
        """verify 写回：真实 _writeback_kb 成功/未命中计数。"""
        monkeypatch.setattr(settings, "agent_verify_loop_kb_writeback_enabled", True)
        context = _real_context()
        fingerprint = context["exception"]["fingerprint"]
        upsert_knowledge_entry(
            fingerprint=fingerprint,
            analysis={"root_cause": "seed"},
            fix_suggestion="seed-fix",
            source="seed",
        )
        ctx = SimpleNamespace(debug_context=context)

        before = get_kb_metric_snapshot()["writeback"]["verify"]
        assert _writeback_kb(ctx, {}, score=0.9) is True
        mid = get_kb_metric_snapshot()["writeback"]["verify"]
        assert mid["success"] == before["success"] + 1

        # 未命中：真实 KB 中不存在的指纹
        miss_ctx = SimpleNamespace(
            debug_context={"exception": {"fingerprint": "no-such-fp"}}
        )
        assert _writeback_kb(miss_ctx, {}, score=0.9) is None
        after = get_kb_metric_snapshot()["writeback"]["verify"]
        assert after["miss"] == before["miss"] + 1

    def test_verify_writeback_skipped_when_disabled(self, monkeypatch):
        """开关关闭（真实 settings 默认路径）→ skipped 计数。"""
        monkeypatch.setattr(settings, "agent_verify_loop_kb_writeback_enabled", False)
        before = get_kb_metric_snapshot()["writeback"]["verify"]
        assert _writeback_kb(SimpleNamespace(debug_context={}), {}, score=0.9) is None
        after = get_kb_metric_snapshot()["writeback"]["verify"]
        assert after["skipped"] == before["skipped"] + 1

    def test_experience_recall_counter(self, monkeypatch):
        """经验召回：真实 assembler 方法 + retriever 注入（数据源唯一注入点）。"""
        context = _real_context()
        assembler = RepairContextAssembler()

        def fake_hit(**kwargs):
            class _R:
                def to_dict(self):
                    return {"fingerprint": "fp-x"}

            return [_R()]

        monkeypatch.setattr(
            "app.rag.retriever.retrieve_debug_experience", fake_hit
        )
        monkeypatch.setattr(settings, "debug_experience_enabled", True)
        before = get_kb_metric_snapshot()["experience_recall"]
        asyncio.run(assembler._safe_debug_experience_recall(context))
        mid = get_kb_metric_snapshot()["experience_recall"]
        assert mid["hit"] == before["hit"] + 1

        monkeypatch.setattr(
            "app.rag.retriever.retrieve_debug_experience", lambda **kw: []
        )
        asyncio.run(assembler._safe_debug_experience_recall(context))
        after = get_kb_metric_snapshot()["experience_recall"]
        assert after["miss"] == before["miss"] + 1


# ---------------------------------------------------------------------------
# 交付物一：/metrics 文本输出
# ---------------------------------------------------------------------------


def test_prometheus_text_contains_kb_metrics():
    """真实 KB 命中后 /metrics 文本包含 kb_* 指标（Prometheus 抓取面）。"""
    context = _real_context()
    upsert_knowledge_entry(
        fingerprint=context["exception"]["fingerprint"],
        analysis={"root_cause": "known"},
        fix_suggestion="f",
        source="llm",
    )
    analyze(context)
    # 真实触发一次分析回写（渲染段按需输出：无数据的指标段不渲染）
    _persist_analysis_to_knowledge_base(
        context["exception"]["fingerprint"],
        {"analysis": {"root_cause": "obs"}},
        context,
    )
    text = _render_prometheus()
    assert "# TYPE kb_hits_total counter" in text
    assert 'kb_hits_total{level="l1_fingerprint"}' in text
    assert "# TYPE kb_writeback_total counter" in text
    assert 'kb_writeback_total{kind="analysis",status="success"}' in text
    assert "# TYPE kb_experience_recall_total counter" in text


# ---------------------------------------------------------------------------
# 交付物二：/api/dashboard/kb-stats 端点
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from app.api.dashboard import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestKbStatsEndpoint:
    def test_empty_store_returns_zeroes(self, client):
        """store 为空 → 零值结构而非 500（失败静默降级纪律）。"""
        resp = client.get("/api/dashboard/kb-stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_entries"] == 0
        assert body["by_source"] == {}
        assert body["learned_entries"] == 0
        assert body["learned_ratio"] == 0.0
        assert body["verified_entries"] == 0
        assert set(body["metrics_snapshot"]["hits_by_level"]) == {
            "l1_fingerprint", "l1_5_normalized", "l2_type", "vector_rag", "miss",
        }
        assert "analysis" in body["metrics_snapshot"]["writeback"]
        assert "verify" in body["metrics_snapshot"]["writeback"]

    def test_entry_distribution_after_writes(self, client):
        """真实 upsert 后：总数 / 来源分布 / 学习占比 / 重复验证计数。"""
        upsert_knowledge_entry(
            fingerprint="fp-seed-1",
            analysis={"root_cause": "a"},
            fix_suggestion="f",
            source="seed",
        )
        upsert_knowledge_entry(
            fingerprint="fp-llm-1",
            analysis={"root_cause": "b"},
            fix_suggestion="f",
            source="llm",
        )
        # 两次真实 verify 写回（record_verification 递增 verify_count）
        from app.rag.knowledge_base import record_verification

        record_verification("fp-llm-1", 0.9)
        record_verification("fp-llm-1", 0.95)

        resp = client.get("/api/dashboard/kb-stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_entries"] == 2
        assert body["by_source"] == {"seed": 1, "llm": 1}
        assert body["learned_entries"] == 1
        assert body["learned_ratio"] == 0.5
        assert body["verified_entries"] == 1  # 仅 fp-llm-1 verify_count=2 > 1

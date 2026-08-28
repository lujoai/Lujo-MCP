"""R7-P1-2 指纹闭环契约测试 —— 全部用真实生产者输出，禁止伪造 fixture。

背景（CR-1 教训）：指纹上游已算好（errors.record / capture_exception /
trace_repo 回读），却经 context builder 丢弃，KB 三级命中 / 向量 RAG /
分析回写 / verify 写回 / 经验召回整条学习闭环因「指纹为空」在生产全死。
本文件的 context 一律来自真实生产链路：
- ``/api/debug/run`` 端点函数真实执行（异常路径经 monkeypatch 触发）；
- ``trace_repo.save_trace`` 真实落库 + 重启回读路径；
不构造与消费者期望同形的 {type, message, frames, fingerprint} 字典。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.api.debug import DebugRequest
from app.config import settings
from app.rag.knowledge_base import (
    clear_knowledge_base,
    get_knowledge_entry,
    upsert_knowledge_entry,
)
from app.runtime.context.builder import build_debug_context
from app.runtime.core import errors, trace_repo
from app.runtime.core.errors import compute_fingerprint

# ---------------------------------------------------------------------------
# 隔离：errors 近期缓冲 + KB 单例
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 断点③：capture_exception 返回结构携带指纹
# ---------------------------------------------------------------------------


class TestCaptureExceptionProducesFingerprint:
    def test_capture_exception_returns_real_fingerprint(self):
        """真实抛异常 → capture_exception 产出的指纹与 compute_fingerprint 算法一致。"""
        from app.runtime.collectors.stacktrace import capture_exception

        try:
            raise ValueError("fingerprint-loop-contract")
        except ValueError:
            data = capture_exception(source="unit")

        assert data["type"] == "ValueError"
        assert data["frames"], "真实异常必有堆栈帧"
        assert data["fingerprint"]
        # 契约：与 errors.record 用同一算法，两条路径产出的指纹可互相对齐
        assert data["fingerprint"] == compute_fingerprint(data["type"], data["frames"])

    def test_run_endpoint_error_path_carries_fingerprint(self, monkeypatch):
        """经 /api/debug/run 真实端点（异常路径注入）：context.exception 与
        errors[0] 均携带真实指纹（此前该路径指纹从未生成）。"""
        import app.api.debug as debug_mod

        real_add_log = debug_mod.add_log

        def exploding_add_log(rid, step, data):
            if step == "processing":
                raise RuntimeError("injected-for-contract-test")
            return real_add_log(rid, step, data)

        monkeypatch.setattr(debug_mod, "add_log", exploding_add_log)

        resp = debug_mod.debug_run(DebugRequest(payload={"k": "v"}))
        context = resp.context.model_dump()

        fingerprint = context["exception"]["fingerprint"]
        assert fingerprint, "/run 异常路径必须产出指纹（R7-P1-2 断点③）"
        assert context["errors"][0]["fingerprint"] == fingerprint
        assert fingerprint == compute_fingerprint(
            context["exception"]["type"], context["exception"]["frames"]
        )


# ---------------------------------------------------------------------------
# 断点①：builder 不再丢弃指纹（含 fallback 合成路径）
# ---------------------------------------------------------------------------


class TestBuilderInjectsFingerprint:
    def test_build_debug_context_injects_fingerprint(self):
        """真实 save_trace 落库 → build_debug_context 注入
        exception / errors[0] / 顶层三处指纹。"""
        error_id = trace_repo.save_trace("ValueError", "builder-contract", _FRAMES, source="t")
        trace = trace_repo.get_trace(error_id)
        assert trace["fingerprint"]

        ctx = build_debug_context(error_id)
        assert ctx is not None
        dumped = ctx.model_dump()
        assert dumped["exception"]["fingerprint"] == trace["fingerprint"]
        assert dumped["errors"][0]["fingerprint"] == trace["fingerprint"]
        assert dumped["fingerprint"] == trace["fingerprint"]

    def test_builder_fallback_synthetic_trace_computes_fingerprint(self, monkeypatch):
        """重启/缓冲淘汰后 fallback 合成路径：不再显式 None，用真实 error 条目
        指纹透传或 compute_fingerprint 现算。"""
        from app.runtime.core.logs import add_log

        rid = "req-fallback-contract"
        add_log(rid, "request_start", {"method": "POST", "url": "/api/x"})
        add_log(rid, "error", {
            "type": "TypeError",
            "message": "fallback-contract",
            "frames": _FRAMES,
            "frame_count": len(_FRAMES),
        })

        # 模拟 errors 缓冲未命中（重启场景）→ 走 fallback 合成
        monkeypatch.setattr(trace_repo, "_get_error", lambda *a, **k: None)
        ctx = build_debug_context(rid)
        assert ctx is not None
        dumped = ctx.model_dump()
        assert dumped["exception"]["fingerprint"] == compute_fingerprint("TypeError", _FRAMES)
        # fallback 携带真实堆栈帧与请求载荷（R7-Q1 修复链路依赖）
        assert dumped["exception"]["frames"] == _FRAMES
        assert dumped["input"] == {"method": "POST", "url": "/api/x"}


# ---------------------------------------------------------------------------
# 断点②：重启/缓冲淘汰回读路径可恢复指纹
# ---------------------------------------------------------------------------


class TestRebuildPathRestoresFingerprint:
    def test_rebuild_uses_persisted_fingerprint(self, monkeypatch):
        error_id = trace_repo.save_trace("ValueError", "rebuild-contract", _FRAMES, source="t")
        expected = trace_repo.get_trace(error_id)["fingerprint"]

        # 模拟重启：errors 内存缓冲未命中
        monkeypatch.setattr(trace_repo, "_get_error", lambda *a, **k: None)
        rebuilt = trace_repo.get_trace(error_id)
        assert rebuilt is not None and rebuilt.get("from_store")
        assert rebuilt["fingerprint"] == expected

    def test_rebuild_legacy_data_recomputes_fingerprint(self, monkeypatch):
        """旧数据（落库时无 fingerprint 字段）回读时按同一算法重算兜底。"""
        from app.runtime.core.logs import add_log

        rid = "err-legacy-contract"
        add_log(rid, "trace_data", {
            "type": "TypeError",
            "message": "legacy",
            "frames": _FRAMES,
            "frame_count": len(_FRAMES),
            "source": "storage",
            "ts": 123.0,
        })
        monkeypatch.setattr(trace_repo, "_get_error", lambda *a, **k: None)
        rebuilt = trace_repo.get_trace(rid)
        assert rebuilt is not None
        assert rebuilt["fingerprint"] == compute_fingerprint("TypeError", _FRAMES)


# ---------------------------------------------------------------------------
# 端到端：真实生产 context → KB 命中 / 回写 / verify 写回 / 经验召回可达
# ---------------------------------------------------------------------------


def _real_context_via_builder() -> dict:
    """真实生产链路产出 context（save_trace → build_debug_context）。"""
    error_id = trace_repo.save_trace("ValueError", "e2e-contract", _FRAMES, source="t")
    return build_debug_context(error_id).model_dump()


class TestLearningLoopReachableWithRealContext:
    def test_kb_exact_hit_reachable(self):
        from app.llm.kb_integration import _get_knowledge_base_result

        context = _real_context_via_builder()
        fingerprint = context["exception"]["fingerprint"]
        assert fingerprint

        upsert_knowledge_entry(
            fingerprint=fingerprint,
            analysis={"root_cause": "known-cause", "confidence": "high"},
            fix_suggestion="known-fix",
            source="llm",
        )
        hit = _get_knowledge_base_result(context)
        assert hit is not None, "真实 context 指纹必须可达 KB L1 命中（修复前恒 None）"
        assert hit["analysis_source"] == "knowledge_base"
        assert hit["analysis"]["root_cause"] == "known-cause"

    def test_analysis_persist_reachable(self):
        from app.llm.kb_integration import _persist_analysis_to_knowledge_base

        context = _real_context_via_builder()
        fingerprint = context["exception"]["fingerprint"]

        _persist_analysis_to_knowledge_base(
            fingerprint, {"analysis": {"root_cause": "llm-says"}}, context
        )
        entry = get_knowledge_entry(fingerprint)
        assert entry is not None, "真实指纹必须可回写 KB（修复前恒 return）"
        assert entry["analysis"]["root_cause"] == "llm-says"
        assert entry["analysis"]["exception_type"] == "ValueError"

    def test_verify_writeback_reachable(self, monkeypatch):
        from app.agent.verify_loop import _writeback_kb

        context = _real_context_via_builder()
        fingerprint = context["exception"]["fingerprint"]
        upsert_knowledge_entry(
            fingerprint=fingerprint,
            analysis={"root_cause": "seed"},
            fix_suggestion="seed-fix",
            source="seed",
        )
        monkeypatch.setattr(settings, "agent_verify_loop_kb_writeback_enabled", True)

        ctx = SimpleNamespace(debug_context=context)
        ok = _writeback_kb(ctx, {}, score=0.9)
        assert ok is True, "真实 context 指纹必须可达 verify 写回（修复前恒 None）"
        assert get_knowledge_entry(fingerprint)["verify_count"] == 1

    def test_debug_experience_recall_receives_fingerprint(self, monkeypatch):
        """context_assembler 经验召回：真实 context 的顶层指纹可达 retriever。"""
        from app.agent import context_assembler

        context = _real_context_via_builder()
        captured = {}

        def fake_retrieve(**kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr("app.rag.retriever.retrieve_debug_experience", fake_retrieve)
        monkeypatch.setattr(settings, "debug_experience_enabled", True)

        result = asyncio.run(
            context_assembler.RepairContextAssembler()._safe_debug_experience_recall(context)
        )
        # 无历史记录时静默降级为 None；关键是真实指纹已传递到 retriever
        assert result is None
        assert captured.get("fingerprint") == context["fingerprint"]

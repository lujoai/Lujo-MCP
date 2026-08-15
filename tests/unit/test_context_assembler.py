"""单元测试：修复上下文装配器（context_assembler.py）。

覆盖：四个子装配并发执行、各自失败静默降级、sources 结构正确。
"""

import contextlib
import pytest
from unittest.mock import patch

from app.agent.context_assembler import RepairContextAssembler


@pytest.fixture
def assembler():
    return RepairContextAssembler()


def _make_debug_context():
    """构造一个含 exception.frames 的 debug context。"""
    return {
        "request_id": "r1",
        "exception": {
            "type": "ValueError",
            "message": "bad input",
            "frames": [
                {"file": "/app/foo.py", "line": 42, "function": "bar"},
                {"file": "/app/baz.py", "line": 10, "function": "qux"},
            ],
        },
    }


def _without_cache_flag(analysis):
    """剔除缓存命中标记：cached 只表示 LLM 分析是否复用缓存，不属内容差异。"""
    if isinstance(analysis, dict):
        return {k: v for k, v in analysis.items() if k != "cached"}
    return analysis


class TestAssembleSuccess:
    """三个子装配都成功 → 完整 sources 结构。"""

    @pytest.mark.asyncio
    async def test_assemble_returns_all_fields(self, assembler):
        ctx = _make_debug_context()
        fake_analysis = {
            "analysis": {"root_cause": "x"},
            "knowledge_base_hit": True,
        }
        fake_vector = [{"fingerprint": "fp1", "analysis": {"root_cause": "similar"}}]
        fake_git_entry = {"file": "/app/foo.py", "diff": "--- a\n+++ b\n"}

        with patch(
            "app.llm.analyzer.analyze_async", return_value=fake_analysis
        ), patch(
            "app.rag.knowledge_base.retrieve_similar", return_value=fake_vector
        ), patch(
            "app.runtime.core.git.get_recent_diff", return_value=fake_git_entry
        ):
            result = await assembler.assemble(ctx)

        assert result["debug_context"] == ctx
        assert result["prior_analysis"] == fake_analysis
        assert result["vector_recall"] == fake_vector
        # debug_context 有 2 帧，git_context 应有 2 个条目（每帧拉一次 diff）
        assert len(result["git_context"]) == 2
        assert all(entry == fake_git_entry for entry in result["git_context"])
        assert result["sources"]["knowledge_base_hit"] is True
        assert result["sources"]["vector_recall"] == fake_vector
        assert len(result["sources"]["git_context"]) == 2


class TestAnalysisDegradation:
    """analyze_async 失败 → prior_analysis=None，继续。"""

    @pytest.mark.asyncio
    async def test_analysis_failure_degrades_to_none(self, assembler):
        ctx = _make_debug_context()

        with patch(
            "app.llm.analyzer.analyze_async", side_effect=RuntimeError("LLM down")
        ), patch(
            "app.rag.knowledge_base.retrieve_similar", return_value=[]
        ), patch(
            "app.runtime.core.git.get_recent_diff", return_value=None
        ):
            result = await assembler.assemble(ctx)

        assert result["prior_analysis"] is None
        assert result["vector_recall"] == []
        assert result["git_context"] == []
        # knowledge_base_hit 应为 False（analysis 为 None）
        assert result["sources"]["knowledge_base_hit"] is False

    @pytest.mark.asyncio
    async def test_analysis_disabled_via_flag(self, assembler, monkeypatch):
        """agent_prior_analysis_enabled=False 时跳过 analyze_async。"""
        monkeypatch.setattr(
            "app.config.settings.agent_prior_analysis_enabled", False
        )
        ctx = _make_debug_context()

        with patch(
            "app.llm.analyzer.analyze_async", side_effect=AssertionError("should not be called")
        ), patch(
            "app.rag.knowledge_base.retrieve_similar", return_value=[]
        ), patch(
            "app.runtime.core.git.get_recent_diff", return_value=None
        ):
            result = await assembler.assemble(ctx)

        assert result["prior_analysis"] is None


class TestVectorRecallDegradation:
    """retrieve_similar 失败 → vector_recall=[]，继续。"""

    @pytest.mark.asyncio
    async def test_vector_failure_degrades_to_empty(self, assembler):
        ctx = _make_debug_context()

        with patch(
            "app.llm.analyzer.analyze_async", return_value=None
        ), patch(
            "app.rag.knowledge_base.retrieve_similar", side_effect=RuntimeError("qdrant down")
        ), patch(
            "app.runtime.core.git.get_recent_diff", return_value=None
        ):
            result = await assembler.assemble(ctx)

        assert result["vector_recall"] == []
        assert result["sources"]["vector_recall"] == []


class TestGitContextDegradation:
    """get_recent_diff 失败 → git_context=[]，继续。"""

    @pytest.mark.asyncio
    async def test_git_failure_degrades_to_empty(self, assembler):
        ctx = _make_debug_context()

        with patch(
            "app.llm.analyzer.analyze_async", return_value=None
        ), patch(
            "app.rag.knowledge_base.retrieve_similar", return_value=[]
        ), patch(
            "app.runtime.core.git.get_recent_diff", side_effect=RuntimeError("git timeout")
        ):
            result = await assembler.assemble(ctx)

        assert result["git_context"] == []

    @pytest.mark.asyncio
    async def test_git_only_first_3_frames(self, assembler):
        """git 上下文只拉前 3 帧，避免串行 git 调用拖慢。"""
        ctx = {
            "request_id": "r1",
            "exception": {
                "frames": [
                    {"file": f"/app/f{i}.py", "line": i, "function": "fn"}
                    for i in range(5)
                ]
            },
        }

        call_count = {"n": 0}

        def fake_get_recent_diff(file_path, commits_back=3):
            call_count["n"] += 1
            return {"file": file_path, "diff": "..."}

        with patch(
            "app.llm.analyzer.analyze_async", return_value=None
        ), patch(
            "app.rag.knowledge_base.retrieve_similar", return_value=[]
        ), patch(
            "app.runtime.core.git.get_recent_diff", side_effect=fake_get_recent_diff
        ):
            result = await assembler.assemble(ctx)

        assert call_count["n"] == 3
        assert len(result["git_context"]) == 3


class TestEmptyContext:
    """空 debug context（无 exception）不崩溃。"""

    @pytest.mark.asyncio
    async def test_no_exception_field(self, assembler):
        ctx = {"request_id": "r1"}  # 无 exception

        with patch(
            "app.llm.analyzer.analyze_async", return_value=None
        ), patch(
            "app.rag.knowledge_base.retrieve_similar", return_value=[]
        ), patch(
            "app.runtime.core.git.get_recent_diff", return_value=None
        ):
            result = await assembler.assemble(ctx)

        assert result["git_context"] == []
        assert result["debug_context"] == ctx


class TestDebugExperienceRecall:
    """debug_experience 子装配（P1）：开关 / 成功 / 降级 / top_k。"""

    @staticmethod
    def _base_patches():
        return (
            patch("app.llm.analyzer.analyze_async", return_value=None),
            patch("app.rag.knowledge_base.retrieve_similar", return_value=[]),
            patch("app.runtime.core.git.get_recent_diff", return_value=None),
        )

    @staticmethod
    def _enter_patches(extra_patch):
        stack = contextlib.ExitStack()
        for p in TestDebugExperienceRecall._base_patches():
            stack.enter_context(p)
        if extra_patch is not None:
            stack.enter_context(extra_patch)
        return stack

    @pytest.mark.asyncio
    async def test_disabled_by_default_skips_retriever(self, assembler, monkeypatch):
        """默认关闭：retriever 不被调用，debug_experience=None。"""
        monkeypatch.setattr("app.config.settings.debug_experience_enabled", False)
        with self._enter_patches(patch(
            "app.rag.retriever.retrieve_debug_experience",
            side_effect=AssertionError("retriever must not be called when disabled"),
        )):
            result = await assembler.assemble(_make_debug_context())
        assert result["debug_experience"] is None

    @pytest.mark.asyncio
    async def test_enabled_returns_experience(self, assembler, monkeypatch):
        """开启：retriever 命中返回 records。"""
        monkeypatch.setattr("app.config.settings.debug_experience_enabled", True)
        from app.rag.experience import DebugExperienceRecord

        fake = DebugExperienceRecord(
            fingerprint="fp-1",
            exception_type="ValueError",
            message_pattern="bad input",
            source="fingerprint",
        )
        with self._enter_patches(patch(
            "app.rag.retriever.retrieve_debug_experience", return_value=[fake]
        )):
            result = await assembler.assemble(_make_debug_context())

        assert result["debug_experience"] is not None
        assert result["debug_experience"][0].fingerprint == "fp-1"
        assert result["debug_experience"][0].source == "fingerprint"

    @pytest.mark.asyncio
    async def test_retriever_exception_degrades(self, assembler, monkeypatch):
        """retriever 抛异常：debug_experience=None，其他字段不受影响。"""
        monkeypatch.setattr("app.config.settings.debug_experience_enabled", True)
        with self._enter_patches(patch(
            "app.rag.retriever.retrieve_debug_experience",
            side_effect=RuntimeError("kb down"),
        )):
            result = await assembler.assemble(_make_debug_context())

        assert result["debug_experience"] is None
        assert result["debug_context"] == _make_debug_context()
        assert result["prior_analysis"] is None
        assert result["vector_recall"] == []
        assert result["git_context"] == []

    @pytest.mark.asyncio
    async def test_existing_fields_unchanged_when_enabled(self, assembler, monkeypatch):
        """开启后已有字段与关闭基线一致。"""
        result_off = await assembler.assemble(_make_debug_context())

        monkeypatch.setattr("app.config.settings.debug_experience_enabled", True)
        with patch("app.rag.retriever.retrieve_debug_experience", return_value=[]):
            result_on = await assembler.assemble(_make_debug_context())

        assert result_on["debug_context"] == result_off["debug_context"]
        assert _without_cache_flag(result_on["prior_analysis"]) == _without_cache_flag(result_off["prior_analysis"])
        assert result_on["vector_recall"] == result_off["vector_recall"]
        assert result_on["git_context"] == result_off["git_context"]
        # 空结果 → None
        assert result_on["debug_experience"] is None

    @pytest.mark.asyncio
    async def test_top_k_passed_correctly(self, assembler, monkeypatch):
        """top_k 与异常特征参数正确传递给 retriever。"""
        monkeypatch.setattr("app.config.settings.debug_experience_enabled", True)
        monkeypatch.setattr("app.config.settings.debug_experience_top_k", 5)

        captured = {}

        def fake_retriever(**kwargs):
            captured.update(kwargs)
            return []

        with self._enter_patches(patch(
            "app.rag.retriever.retrieve_debug_experience", side_effect=fake_retriever
        )):
            await assembler.assemble(_make_debug_context())

        assert captured["top_k"] == 5
        assert captured["exc_type"] == "ValueError"
        assert captured["message"] == "bad input"
        assert captured["debug_context"] is not None

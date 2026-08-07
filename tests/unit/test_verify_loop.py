"""单元测试：Verify Loop 四级判定 + 综合评分 + 迭代闭环 + KB 写回（M4）。"""
import pytest


class TestVerifyVerdict:
    def test_high_confidence_threshold(self):
        from app.agent.verify_loop import compute_verdict, VerifyVerdict

        assert compute_verdict(0.9) == VerifyVerdict.HIGH_CONFIDENCE
        assert compute_verdict(0.85) == VerifyVerdict.HIGH_CONFIDENCE

    def test_passed_threshold(self):
        from app.agent.verify_loop import compute_verdict, VerifyVerdict

        assert compute_verdict(0.7) == VerifyVerdict.PASSED
        assert compute_verdict(0.75) == VerifyVerdict.PASSED

    def test_partial_threshold(self):
        from app.agent.verify_loop import compute_verdict, VerifyVerdict

        assert compute_verdict(0.5) == VerifyVerdict.PARTIAL
        assert compute_verdict(0.6) == VerifyVerdict.PARTIAL

    def test_failed_below_threshold(self):
        from app.agent.verify_loop import compute_verdict, VerifyVerdict

        assert compute_verdict(0.4) == VerifyVerdict.FAILED
        assert compute_verdict(0.0) == VerifyVerdict.FAILED


class TestVerifyScore:
    def test_full_score(self):
        from app.agent.verify_loop import compute_verify_score

        result = {
            "repair_plan": {"fix": "x"},
            "test_plan": {"test_cases": ["t1"]},
            "security_review": {"findings": [{"severity": "low"}]},
            "git_attribution": {"file": "a.py"},
        }
        assert compute_verify_score(result) == 1.0

    def test_repair_only(self):
        from app.agent.verify_loop import compute_verify_score

        assert compute_verify_score({"repair_plan": {"fix": "x"}}) == 0.4

    def test_security_critical_no_score(self):
        from app.agent.verify_loop import compute_verify_score

        result = {
            "repair_plan": {"fix": "x"},
            "security_review": {"findings": [{"severity": "critical"}]},
        }
        # repair 0.4 + security 0（critical 不加分）
        assert compute_verify_score(result) == 0.4

    def test_empty_result_zero(self):
        from app.agent.verify_loop import compute_verify_score

        assert compute_verify_score({}) == 0.0


class TestRunVerifyLoop:
    def test_loop_converges_on_pass(self, monkeypatch):
        """通过后立即收敛，不再进入下一轮。"""
        from app.agent.verify_loop import run_verify_loop

        calls = {"n": 0}

        async def iteration_fn(ctx, sources):
            calls["n"] += 1
            return {
                "repair_plan": {"fix": "x"},
                "test_plan": {"test_cases": ["t1"]},
                "security_review": {"findings": [{"severity": "low"}]},
                "git_attribution": {"file": "a.py"},
            }

        class Ctx:
            def __init__(self):
                self.repair_context = {}

        result = run_verify_loop(iteration_fn, Ctx(), {})
        import asyncio

        result = asyncio.run(result)
        assert calls["n"] == 1
        assert result["verify_loop"]["final_verdict"] == "high_confidence"
        assert result["verify_loop"]["total_iterations"] == 1

    def test_loop_partial_retries_until_max(self, monkeypatch):
        """partial 会继续迭代直到最大轮数。"""
        from app.agent.verify_loop import run_verify_loop

        calls = {"n": 0}

        async def iteration_fn(ctx, sources):
            calls["n"] += 1
            # repair 0.4 + git 0.1 = 0.5 → partial（不满足 passed，继续迭代）
            return {"repair_plan": {"fix": "x"}, "git_attribution": {"file": "a.py"}}

        class Ctx:
            def __init__(self):
                self.repair_context = {}

        import asyncio

        result = asyncio.run(run_verify_loop(iteration_fn, Ctx(), {}))
        assert calls["n"] == 3  # 默认 max_iterations=3
        assert result["verify_loop"]["total_iterations"] == 3
        assert result["verify_loop"]["final_verdict"] == "partial"

    def test_kb_writeback_on_pass(self, monkeypatch):
        """通过后写回 KB（verify_count 递增）。"""
        from app.agent.verify_loop import run_verify_loop
        from app.rag.knowledge_base import (
            clear_knowledge_base,
            upsert_knowledge_entry,
            get_knowledge_entry,
        )

        clear_knowledge_base()
        upsert_knowledge_entry(
            fingerprint="fp-verify",
            analysis={"exception_type": "ValueError", "message": "boom"},
            fix_suggestion="fix",
            source="test",
        )

        async def iteration_fn(ctx, sources):
            return {
                "repair_plan": {"fix": "x"},
                "test_plan": {"test_cases": ["t1"]},
                "security_review": {"findings": [{"severity": "low"}]},
                "git_attribution": {"file": "a.py"},
            }

        class Ctx:
            def __init__(self):
                self.repair_context = {}
                self.debug_context = {
                    "exception": {"fingerprint": "fp-verify", "type": "ValueError"}
                }

        import asyncio

        result = asyncio.run(run_verify_loop(iteration_fn, Ctx(), {}))
        assert result["verify_loop"]["kb_writeback"] is True
        entry = get_knowledge_entry("fp-verify")
        assert entry["verify_count"] == 1
        assert entry["case_confidence"] > 0

    def test_kb_writeback_disabled(self, monkeypatch):
        """关闭写回时返回 None。"""
        from app.agent.verify_loop import run_verify_loop
        from app.config import settings

        saved = settings.agent_verify_loop_kb_writeback_enabled
        settings.agent_verify_loop_kb_writeback_enabled = False
        try:

            async def iteration_fn(ctx, sources):
                return {
                    "repair_plan": {"fix": "x"},
                    "test_plan": {"test_cases": ["t1"]},
                    "security_review": {"findings": [{"severity": "low"}]},
                    "git_attribution": {"file": "a.py"},
                }

            class Ctx:
                def __init__(self):
                    self.repair_context = {}
                    self.debug_context = {
                        "exception": {"fingerprint": "fp-verify"}
                    }

            import asyncio

            result = asyncio.run(run_verify_loop(iteration_fn, Ctx(), {}))
            assert result["verify_loop"]["kb_writeback"] is None
        finally:
            settings.agent_verify_loop_kb_writeback_enabled = saved

    def test_loop_round_timeout_degrades_to_failed(self, monkeypatch):
        """R4：单轮超时后静默降级为 failed，不阻塞整体。"""
        from app.agent.verify_loop import run_verify_loop
        from app.config import settings

        saved = settings.agent_verify_loop_round_timeout
        monkeypatch.setattr(settings, "agent_verify_loop_round_timeout", 0.05)
        try:
            async def iteration_fn(ctx, sources):
                await asyncio.sleep(1)  # 远超单轮超时
                return {"repair_plan": {"fix": "x"}}

            class Ctx:
                def __init__(self):
                    self.repair_context = {}

            import asyncio

            result = asyncio.run(run_verify_loop(iteration_fn, Ctx(), {}))
            # 每轮都超时 → 全部判定 failed，但循环不被单轮卡死，跑满 max_iterations
            assert result["verify_loop"]["final_verdict"] == "failed"
            assert result["verify_loop"]["total_iterations"] == 3
        finally:
            settings.agent_verify_loop_round_timeout = saved

    def test_loop_round_timeout_zero_disables_timeout(self, monkeypatch):
        """R4：round_timeout=0 不设单轮超时（向后兼容）。"""
        from app.agent.verify_loop import run_verify_loop
        from app.config import settings

        saved = settings.agent_verify_loop_round_timeout
        monkeypatch.setattr(settings, "agent_verify_loop_round_timeout", 0)
        try:
            async def iteration_fn(ctx, sources):
                # 带异步等待的合法结果（若误套超时 0 会被立即取消）
                await asyncio.sleep(0.01)
                return {
                    "repair_plan": {"fix": "x"},
                    "test_plan": {"test_cases": ["t1"]},
                    "security_review": {"findings": [{"severity": "low"}]},
                    "git_attribution": {"file": "a.py"},
                }

            class Ctx:
                def __init__(self):
                    self.repair_context = {}

            import asyncio

            result = asyncio.run(run_verify_loop(iteration_fn, Ctx(), {}))
            assert result["verify_loop"]["final_verdict"] == "high_confidence"
            assert result["verify_loop"]["total_iterations"] == 1
        finally:
            settings.agent_verify_loop_round_timeout = saved
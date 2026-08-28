"""单元测试：Verify Loop 四级判定 + 综合评分 + 迭代闭环 + KB 写回（M4）。"""


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
            "security_review": {"risks": [], "recommendations": [], "overall_severity": "none", "summary": "ok"},
            "git_attribution": {"file": "a.py"},
        }
        assert compute_verify_score(result) == 1.0

    def test_repair_only(self):
        from app.agent.verify_loop import compute_verify_score

        assert compute_verify_score({"repair_plan": {"fix": "x"}}) == 0.4

    def test_security_high_risk_no_score(self):
        from app.agent.verify_loop import compute_verify_score

        result = {
            "repair_plan": {"fix": "x"},
            "security_review": {
                "risks": [{"category": "SSRF", "severity": "high", "description": "d", "location": "l"}],
                "recommendations": ["r"],
                "overall_severity": "high",
                "summary": "s",
            },
        }
        # repair 0.4 + security 0（high 风险不加分）
        assert compute_verify_score(result) == 0.4

    def test_empty_result_zero(self):
        from app.agent.verify_loop import compute_verify_score

        assert compute_verify_score({}) == 0.0


class TestSecurityGateContract:
    """CR-1 契约测试：SecurityAgent 真实输出 shape → compute_verify_score 安全门。

    此前 fixture 用不存在的 "findings" 键自证清白，掩盖了安全门字段错配。
    """

    @staticmethod
    def _make_result(review: dict) -> dict:
        return {
            "repair_plan": {"fix": "x"},
            "test_plan": {"test_cases": ["t1"]},
            "security_review": review,
            "git_attribution": {"file": "a.py"},
        }

    def test_clean_review_from_real_agent_output_scores_full(self):
        """_validate_security_review 的无风险输出 → 安全门通过，满分。"""
        import json

        from app.agent.security_agent import _validate_security_review
        from app.agent.verify_loop import compute_verify_score

        review = _validate_security_review(json.dumps({
            "risks": [],
            "recommendations": [],
            "overall_severity": "none",
            "summary": "no obvious risk",
        }))
        assert compute_verify_score(self._make_result(review)) == 1.0

    def test_high_risk_review_clamped_to_partial(self):
        """_validate_security_review 的高风险输出 → 0.2 不给且钳制到 PARTIAL 阈值。"""
        import json

        from app.agent.security_agent import _validate_security_review
        from app.agent.verify_loop import compute_verify_score
        from app.config import settings

        review = _validate_security_review(json.dumps({
            "risks": [{"category": "SSRF", "severity": "high",
                       "description": "d", "location": "l"}],
            "recommendations": ["r"],
            "overall_severity": "high",
            "summary": "s",
        }))
        # repair 0.4 + test 0.3 + git 0.1 = 0.8 ≥ pass 阈值，但安全门未过 → 钳制
        score = compute_verify_score(self._make_result(review))
        assert score == settings.agent_verify_loop_partial_threshold

    def test_critical_risk_llm_output_clamped_to_partial(self):
        """R7-S1 回归：LLM 原样输出 severity="critical" → 必须钳制到 PARTIAL。

        修复前 _validate_security_review 把 critical 降为 "low"，该端到端路径
        下安全门通过、给出满分 —— verify_loop 的 critical 分支是不可达代码。
        """
        import json

        from app.agent.security_agent import _validate_security_review
        from app.agent.verify_loop import compute_verify_score
        from app.config import settings

        review = _validate_security_review(json.dumps({
            "risks": [{"category": "SSRF", "severity": "critical",
                       "description": "d", "location": "l"}],
            "recommendations": ["r"],
            "overall_severity": "medium",
            "summary": "s",
        }))
        assert review["risks"][0]["severity"] == "high"  # critical→high（fail-safe）
        score = compute_verify_score(self._make_result(review))
        assert score == settings.agent_verify_loop_partial_threshold

    def test_invalid_overall_severity_fails_gate(self):
        """overall_severity 非法值（归一为 unknown）→ fail-safe 不通过安全门。"""
        import json

        from app.agent.security_agent import _validate_security_review
        from app.agent.verify_loop import compute_verify_score
        from app.config import settings

        review = _validate_security_review(json.dumps({
            "risks": [],
            "recommendations": [],
            "overall_severity": "香蕉皮",  # 非法值 → unknown
            "summary": "s",
        }))
        assert review["overall_severity"] == "unknown"
        score = compute_verify_score(self._make_result(review))
        assert score == settings.agent_verify_loop_partial_threshold

    def test_malformed_findings_shape_fails_gate(self):
        """畸形 security_review（旧 findings 形态，无 risks/overall_severity 键）→ 门不通过。"""
        from app.agent.verify_loop import compute_verify_score
        from app.config import settings

        legacy = self._make_result({"findings": [{"severity": "high"}]})
        score = compute_verify_score(legacy)
        assert score == settings.agent_verify_loop_partial_threshold


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
                "security_review": {"risks": [], "recommendations": [], "overall_severity": "none", "summary": "ok"},
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
                "security_review": {"risks": [], "recommendations": [], "overall_severity": "none", "summary": "ok"},
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
                    "security_review": {"risks": [], "recommendations": [], "overall_severity": "none", "summary": "ok"},
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
                    "security_review": {"risks": [], "recommendations": [], "overall_severity": "none", "summary": "ok"},
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

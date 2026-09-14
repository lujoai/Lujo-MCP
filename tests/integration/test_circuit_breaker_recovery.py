"""集成测试：熔断器恢复链路（open -> half-open -> close）。"""

import time
from unittest.mock import MagicMock

import pybreaker
import pytest


@pytest.fixture(autouse=True)
def reset_breakers():
    import app.llm.analyzer as analyzer_module
    import app.llm.cache as cache_module

    cache_module._analysis_cache.clear()
    if analyzer_module._llm_circuit_breaker:
        analyzer_module._llm_circuit_breaker.close()
    analyzer_module._llm_circuit_breaker = None

    yield

    cache_module._analysis_cache.clear()
    if analyzer_module._llm_circuit_breaker:
        analyzer_module._llm_circuit_breaker.close()
    analyzer_module._llm_circuit_breaker = None


@pytest.mark.integration
def test_llm_circuit_breaker_recovers_after_reset_timeout(monkeypatch):
    import app.llm.analyzer as analyzer_module

    monkeypatch.setattr(analyzer_module, "pybreaker", pybreaker)
    cb = pybreaker.CircuitBreaker(
        fail_max=1,
        reset_timeout=0.2,
        exclude=[pybreaker.CircuitBreakerError],
    )
    analyzer_module._llm_circuit_breaker = cb

    success_result = {
        "analysis": {
            "root_cause": "recovered",
            "impact": "low",
            "fix": "none",
            "confidence": "high",
        },
        "model": "mock-model",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "attempts": 1,
    }

    calls = {"n": 0}

    def fake_retry_call(client, model_name, messages, temperature, max_retries):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("llm unavailable")
        return success_result.copy()

    monkeypatch.setattr(analyzer_module, "_retry_call", fake_retry_call)
    monkeypatch.setattr(analyzer_module, "_get_client", lambda: object())

    ctx = {"request_id": "cb-recovery-001", "errors": ["boom"]}

    first = analyzer_module.analyze(ctx)
    assert first["_circuit_breaker_triggered"] is True
    assert cb.current_state == "open"

    second = analyzer_module.analyze({"request_id": "cb-recovery-002", "errors": ["boom"]})
    assert second["_circuit_breaker_triggered"] is True
    assert calls["n"] == 1

    time.sleep(0.25)

    recovered = analyzer_module.analyze({"request_id": "cb-recovery-003", "errors": ["boom"]})
    assert recovered["analysis"]["root_cause"] == "recovered"
    assert recovered["cached"] is False
    assert cb.current_state == "closed"
    assert calls["n"] == 2

"""SecurityAgent 严重等级校验测试 —— 验证 VALID_SEVERITY 包含 "unknown" """

import json
from app.agent.security_agent import _validate_security_review, VALID_SEVERITY


def _make_review(severity: str) -> str:
    """构造 LLM 返回的 JSON 字符串"""
    return json.dumps({
        "overall_severity": severity,
        "risks": [],
        "recommendations": [],
    })


def test_invalid_severity_mapped_to_unknown_sentinel():
    """'unknown' 是 VALID_SEVERITY 之外的哨兵值：无效 LLM 值应归为 'unknown' 而非泄漏原始值"""
    assert "unknown" not in VALID_SEVERITY
    result = _validate_security_review(_make_review("invalid_value"))
    assert result["overall_severity"] == "unknown"


def test_validate_normal_severity():
    """正常严重等级应通过校验"""
    for sev in ("high", "medium", "low", "none"):
        result = _validate_security_review(_make_review(sev))
        assert result["overall_severity"] == sev


def test_validate_unknown_severity():
    """LLM 返回 'unknown' 应保留"""
    result = _validate_security_review(_make_review("unknown"))
    assert result["overall_severity"] == "unknown"


def test_validate_invalid_severity_maps_to_unknown():
    """LLM 返回无效值应映射为 'unknown'"""
    result = _validate_security_review(_make_review("invalid_value"))
    assert result["overall_severity"] == "unknown"


def test_validate_empty_severity_maps_to_none():
    """LLM 返回空字符串应映射为 'none'"""
    result = _validate_security_review(
        json.dumps({"overall_severity": "", "risks": [], "recommendations": []})
    )
    assert result["overall_severity"] == "none"


def test_validate_missing_severity_defaults_to_none():
    """缺失 overall_severity 应默认为 'none'"""
    result = _validate_security_review(
        json.dumps({"risks": [], "recommendations": []})
    )
    assert result["overall_severity"] == "none"

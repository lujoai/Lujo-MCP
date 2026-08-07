"""DebugExperienceRecord 单测：字段映射 / 空降级 / 向后兼容（P1 D1）。"""
import pytest

from app.rag.experience import DebugExperienceRecord


# ── from_kb_entry：字段转换 ──


def test_from_kb_entry_maps_all_fields():
    """典型 KB entry → 9 字段全部正确映射。"""
    entry = {
        "fingerprint": "fp-1",
        "fix_suggestion": "add null check before len()",
        "case_confidence": 0.9,
        "verify_count": 3,
        "analysis": {
            "exception_type": "TypeError",
            "message": "object of type 'NoneType' has no len()",
            "root_cause": "returned None from upstream",
            "source_files": ["app/x.py", "app/y.py"],
        },
    }
    rec = DebugExperienceRecord.from_kb_entry(entry)

    assert rec.fingerprint == "fp-1"
    assert rec.exception_type == "TypeError"
    # message 归一化：带引号的字符串字面量（如 'NoneType'）会被剥离
    assert rec.message_pattern == "object of type has no len()"
    assert rec.solution == "add null check before len()"
    assert rec.fault_location == ["app/x.py", "app/y.py"]
    assert rec.analysis["root_cause"] == "returned None from upstream"
    assert rec.verification_result == "verified(3次, confidence=0.90)"
    assert rec.confidence == 0.9
    # debug_context_summary：KB entry 不含该字段 → 默认空
    assert rec.debug_context_summary == ""


def test_from_kb_entry_normalizes_message_pattern():
    """message_pattern 使用归一化文本（剥离变量值）。"""
    entry = {
        "fingerprint": "fp-2",
        "analysis": {
            "exception_type": "KeyError",
            "message": "'user_id' not found at 0x7f8a1b2c 1234",
        },
    }
    rec = DebugExperienceRecord.from_kb_entry(entry)
    # 带引号的 'user_id' 与 hex 地址 / 数字均被剥离
    assert rec.message_pattern == "not found at"
    assert "0x7f8a1b2c" not in rec.message_pattern
    assert "1234" not in rec.message_pattern


def test_from_kb_entry_solution_fallback_to_analysis():
    """solution 优先顶层 fix_suggestion，缺失时回退 analysis 内嵌值。"""
    entry = {"fingerprint": "fp-3", "analysis": {"fix_suggestion": "fallback fix"}}
    rec = DebugExperienceRecord.from_kb_entry(entry)
    assert rec.solution == "fallback fix"


def test_to_dict_roundtrip():
    """to_dict 输出 9 个键，且为副本（不共享引用）。"""
    rec = DebugExperienceRecord(
        fingerprint="fp",
        exception_type="ValueError",
        analysis={"root_cause": "rc"},
        fault_location=["a.py"],
    )
    d = rec.to_dict()
    assert set(d.keys()) == {
        "fingerprint", "exception_type", "message_pattern", "debug_context_summary",
        "fault_location", "analysis", "solution", "verification_result", "confidence",
    }
    d["analysis"]["x"] = 1
    d["fault_location"].append("b.py")
    assert "x" not in rec.analysis
    assert "b.py" not in rec.fault_location


# ── 空字段降级 ──


def test_from_kb_entry_none_returns_default():
    """None / {} entry → 默认值，不抛异常。"""
    rec = DebugExperienceRecord.from_kb_entry(None)
    assert rec.fingerprint == ""
    assert rec.exception_type == ""
    assert rec.message_pattern == ""
    assert rec.fault_location == []
    assert rec.analysis == {}
    assert rec.solution == ""
    assert rec.verification_result == "unverified"
    assert rec.confidence == 0.0


def test_from_kb_entry_analysis_not_dict_degrades():
    """analysis 非 dict（如 None/字符串）→ 静默降级。"""
    rec = DebugExperienceRecord.from_kb_entry({"fingerprint": "fp", "analysis": None})
    assert rec.analysis == {}
    rec2 = DebugExperienceRecord.from_kb_entry({"analysis": "not-a-dict"})
    assert rec2.analysis == {}


def test_from_debug_context_none_returns_default():
    """None debug_context → 默认记录，不抛异常。"""
    rec = DebugExperienceRecord.from_debug_context(None)
    assert rec.exception_type == ""
    assert rec.fault_location == []


# ── backward compatibility ──


def test_from_kb_entry_compatible_with_meta_form():
    """兼容 `_kb_meta` 形态：exception_type/message 在 meta 而非 analysis。"""
    entry = {
        "fingerprint": "fp-4",
        "analysis": {"root_cause": "rc"},
        "_kb_meta": {"exception_type": "RuntimeError", "message": "boom"},
    }
    # 当前实现以 analysis 为权威；meta 形态若 analysis 缺字段则降级为空，
    # 保证不抛异常（向后兼容）。不强制从 meta 取值。
    rec = DebugExperienceRecord.from_kb_entry(entry)
    assert rec.analysis["root_cause"] == "rc"
    assert rec.exception_type == ""
    assert rec.verification_result == "unverified"


def test_from_kb_entry_verify_stats_compat():
    """verify_count/case_confidence 缺省 → unverified + 0.0；异常值不抛错。"""
    rec = DebugExperienceRecord.from_kb_entry({"fingerprint": "fp-5"})
    assert rec.verification_result == "unverified"
    assert rec.confidence == 0.0

    rec2 = DebugExperienceRecord.from_kb_entry(
        {"case_confidence": "oops", "verify_count": "many"}
    )
    assert rec2.confidence == 0.0
    assert rec2.verification_result == "unverified"


# ── from_debug_context 特征提取 ──


def test_from_debug_context_extracts_features():
    """从 build_debug_context 输出提取 exc_type/message/fault_location/summary。"""
    ctx = {
        "exception": {"type": "ValueError", "message": "bad value"},
        "extra": {"fingerprint": "ctx-fp"},
        "fault_localization": {
            "suspicious_frames": [
                {"file": "app/config.py", "function": "Settings", "line": 9},
                {"file": "app/config.py", "function": "Settings", "line": 9},
                {"file": "app/main.py", "function": "run", "line": 3},
            ]
        },
        "static_analysis": {"file": "app/config.py", "function": "Settings"},
    }
    rec = DebugExperienceRecord.from_debug_context(ctx)

    assert rec.fingerprint == "ctx-fp"
    assert rec.exception_type == "ValueError"
    assert "bad value" in rec.message_pattern
    # 去重
    assert rec.fault_location == ["app/config.py", "app/main.py"]
    assert "type=ValueError" in rec.debug_context_summary
    assert "app/config.py" in rec.debug_context_summary


def test_from_debug_context_no_fault_localization():
    """无 fault_localization 时 fault_location 回退 static_analysis / 为空。"""
    ctx = {"exception": {"type": "E", "message": "m"}}
    rec = DebugExperienceRecord.from_debug_context(ctx)
    assert rec.fault_location == []
    assert rec.debug_context_summary == "type=E | message=m"

    ctx2 = {
        "exception": {"type": "E", "message": "m"},
        "static_analysis": {"file": "app/a.py", "function": "f"},
    }
    rec2 = DebugExperienceRecord.from_debug_context(ctx2)
    assert rec2.fault_location == ["app/a.py"]

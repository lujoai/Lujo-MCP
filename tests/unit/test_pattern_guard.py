"""U05-QDRANT-FIX：pattern_guard 共享守卫单测。

背景：`redaction_extra_patterns` 有两个消费方（runtime/core/redaction.py 与
rag/qdrant_vector_store.py 内联副本），后者缺少危险正则检测，形成生产旁路。
检测与编译过滤逻辑下沉到无状态纯工具层 app/utils/pattern_guard.py，两消费方
共用。本文件锁住共享助手的契约：

- 危险形态（嵌套量词 / 重叠重复）被过滤，不进规则列表；
- 非法正则保持既有 warning + skip 语义（warning 含模式原文，与两侧历史行为一致）；
- 危险正则的 warning 摘要不泄露模式内容（只有行号与长度）；
- 安全正则正常编译，替换串默认 ***。
"""
import re

import pytest

from app.utils.pattern_guard import compile_extra_rules, is_dangerous_pattern

_DANGEROUS = [
    r"(a+)+b",
    r"(a|a)*$",
    r"(\w+)+",
    r"(a*)*b",
    r"([a-z]+)+x",
]

_SAFE = [
    r"\b\d{17}[\dXx]\b",
    r"\d{3}-\d{4}",
    r"(?i)token[:=]\s*\S+",
    r"[a-z]+@[a-z]+\.[a-z]{2,}",
    r"(?:foo|bar)-baz",
    r"prefix-\w+",
]


# ── is_dangerous_pattern 形态判定 ──────────────────────────────────


@pytest.mark.parametrize("pattern", _DANGEROUS)
def test_is_dangerous_true_for_nested_quantifier(pattern):
    assert is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", _SAFE)
def test_is_dangerous_false_for_safe_shapes(pattern):
    assert not is_dangerous_pattern(pattern)


# ── compile_extra_rules 过滤与编译 ─────────────────────────────────


@pytest.mark.parametrize("pattern", _DANGEROUS)
def test_dangerous_filtered_out_of_rules(pattern):
    result = compile_extra_rules(pattern)
    assert result.rules == (), f"危险正则未被过滤: {pattern!r}"


@pytest.mark.parametrize("pattern", _SAFE)
def test_safe_compiled_with_default_replacement(pattern):
    result = compile_extra_rules(pattern)
    assert len(result.rules) == 1
    compiled, replacement = result.rules[0]
    assert replacement == "***"
    assert isinstance(compiled, re.Pattern)


def test_invalid_pattern_skipped_warning_keeps_original_semantics():
    """非法正则：跳过 + warning 含模式原文（保持两侧既有行为）。"""
    result = compile_extra_rules("(unclosed")
    assert result.rules == ()
    assert len(result.warnings) == 1
    assert "无效的脱敏正则" in result.warnings[0]
    assert "(unclosed" in result.warnings[0]


def test_dangerous_warning_is_safe_summary_no_pattern_leak():
    """危险正则 warning：只报行号与长度，不输出模式内容。"""
    pattern = r"(a+)+b"
    result = compile_extra_rules(f"\n{pattern}")
    assert result.rules == ()
    assert len(result.warnings) == 1
    msg = result.warnings[0]
    assert pattern not in msg, "warning 泄漏了完整正则内容"
    assert "第 2 行" in msg
    assert str(len(pattern)) in msg


def test_blank_and_whitespace_lines_ignored():
    result = compile_extra_rules("\n  \n\\d{3}-\\d{4}  \n\n")
    assert len(result.rules) == 1
    assert result.warnings == ()


def test_mixed_config_keeps_safe_drops_dangerous_and_invalid():
    raw = "\n".join([r"\d{3}-\d{4}", r"(a+)+b", "(unclosed", r"prefix-\w+"])
    result = compile_extra_rules(raw)
    assert len(result.rules) == 2
    assert len(result.warnings) == 2


def test_empty_raw_returns_no_rules():
    result = compile_extra_rules("")
    assert result.rules == ()
    assert result.warnings == ()

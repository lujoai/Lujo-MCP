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


# ── S3：可证明互斥的分支不过滤，不确定的分支按危险处理 ───────────────

_S3_DANGEROUS = [
    r"(a+)+",
    r"(a*)*",
    r"(\w+)+",
    r"(\d+)+",
    r"([a-z]+)+",
    r"(a|a)*",
    r"(a|a|a)*",
    r"(a|ab)*",
    r"(a|ab|abc)*",
    r"(\w|\d)+",
    r"(a+|b+)+",
    # S3-R2：嵌套无界重复——内层单独看可证明互斥，被外层无界量词再重复后
    # 每次迭代长度可变，与外层产生指数级分段歧义（S3 曾放行，属回归）。
    r"((a|b)+)+",
    r"(?:(a|b)+)+",
    r"((a|b)+)*",
    r"((ab|cd)+)+x",
    # S3-R2：无界 {n,} 的危险侧（此前只有安全侧用例）
    r"(a|a){2,}",
    r"(a|ab){3,}",
    # S3-R2 C4(a)：组内量词 + 组外无界 {n,}，与 (a+)+ 同为嵌套量词灾难形态
    r"(a+){2,}",
    r"(\w+){3,}",
    r"([a-z]+){2,}",
]

_S3_SAFE = [
    r"sk-(live|test)+",
    r"(x|y)+",
    r"(GET|POST)+",
    r"(Bearer|Basic)+",
    r"(foo|bar)+",
    r"(a|b)+",
    r"(ab|cd)+",
    r"(\d{1,3}.){3}\d{1,3}",
    r"(\d{1,3}\.){3}\d{1,3}",
    r"(https?://\S+)",
    r"([a-z]+)",
    r"(a|b)?",
    r"(live|test)",
    # S3-R2 边界：嵌套组但内层无无界量词 / 外层无无界量词 / 外层有界
    r"((a|b)c)+",
    r"(x(y)+)",
    r"((?:ab)+)",
    r"(\d+){3}",
]


@pytest.mark.parametrize("pattern", _S3_DANGEROUS)
def test_s3_contract_dangerous_patterns(pattern):
    assert is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", _S3_SAFE)
def test_s3_contract_safe_patterns(pattern):
    assert not is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"(a|a|a)*", r"(a|ab|abc)*"])
def test_s3_multibranch_overlap_is_dangerous(pattern):
    assert is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"sk-(live|test)+", r"(GET|POST)+", r"(Bearer|Basic)+"])
def test_s3_disjoint_literal_branches_are_not_dangerous(pattern):
    assert not is_dangerous_pattern(pattern)


def test_s3_escaped_pipe_is_not_a_top_level_alternative():
    assert not is_dangerous_pattern(r"(\|)+")


def test_s3_nested_alternative_is_not_split_as_outer_alternative():
    assert not is_dangerous_pattern(r"((a|b)c)+")


def test_s3_nested_alternative_with_outer_branch_is_unprovable():
    assert is_dangerous_pattern(r"((a|b)c|d)+")


@pytest.mark.parametrize("pattern", [r"([|a|b])+", r"(ab|cd){2,3}"])
def test_s3_character_class_and_bounded_repeats_do_not_create_candidates(pattern):
    assert not is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"(ab|cd){2,}", r"(a|b){2,}"])
def test_s3_unbounded_brace_is_analyzed_like_star_plus(pattern):
    """无界 {n,} 是候选形态，与 * / + 同口径分析；纯字面互斥分支放行。

    S3-R2：{2,} 原挂在 "bounded repeats" 用例下，该断言在"正确分析后证明
    安全"与"整体跳过 {n,}"两种实现下都会通过，回归检测能力为零，故拆出。
    """
    assert not is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"(?i)(a|A)+", r"(?i:(a|A)+)"])
def test_s3_inline_flags_prevent_literal_disjointness_proof(pattern):
    assert is_dangerous_pattern(pattern)


def test_s3_dropped_metadata_records_line_and_reason_without_dangerous_pattern():
    dangerous = r"(a|a|a)*"
    result = compile_extra_rules(f"\n{dangerous}\n(unclosed")

    assert result.dropped == ((2, "dangerous"), (3, "invalid"))
    assert dangerous not in result.warnings[0]


def test_s3_guard_result_dropped_defaults_to_empty():
    from app.utils.pattern_guard import GuardResult

    assert GuardResult((), ()).dropped == ()


@pytest.mark.parametrize("consumer", ["runtime", "qdrant"])
def test_s3_consumers_summarize_dropped_rule_count(consumer, monkeypatch, caplog):
    import logging

    from app.config import settings

    monkeypatch.setattr(settings, "redaction_extra_patterns", "(a+)+\n(unclosed")
    if consumer == "runtime":
        from app.runtime.core import redaction

        monkeypatch.setattr(redaction, "_extra_cache", None)
        monkeypatch.setattr(redaction, "_extra_signature", None)
        load_rules = redaction._load_extra_rules
    else:
        from app.rag import qdrant_vector_store

        monkeypatch.setattr(qdrant_vector_store, "_extra_rules_cache", None)
        monkeypatch.setattr(qdrant_vector_store, "_extra_rules_signature", None)
        load_rules = qdrant_vector_store._load_extra_redact_rules

    with caplog.at_level(logging.WARNING):
        load_rules()

    assert "count=2" in caplog.text
    assert "reasons=dangerous=1,invalid=1" in caplog.text, "汇总缺少原因明细"
    assert "(a+)+" not in caplog.text, "日志泄漏了危险模式原文"


# ── S3-R2：嵌套无界重复 fail-closed（C1 回归防线） ─────────────────────


_S3R2_NESTED_DANGEROUS = [
    r"((a|b)+)+",
    r"(?:(a|b)+)+",
    r"((a|b)+)*",
    r"((ab|cd)+)+x",
]


@pytest.mark.parametrize("pattern", _S3R2_NESTED_DANGEROUS)
def test_s3r2_nested_unbounded_repeat_is_dangerous(pattern):
    """外层无界重复包裹'内层带无界量词的组'时必须拦截。

    单独看内层 (a|b)+ 可证明安全（分支纯字面且互斥，每次迭代固定吞 1 字符）；
    但外层再叠一层无界重复后，内层每次可吞任意长度 run，与外层重复产生指数级
    分段歧义（实测 ((a|b)+)+$ 对 33 字符失败输入 383s）。旧规则②靠对内层
    (a|b)+ 的误判意外提供了这层覆盖，S3 修误判时拆掉了它，本用例防回归。
    """
    assert is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"((a|b)+)", r"((a|b))", r"((a|b)+)x"])
def test_s3r2_single_layer_safe_alternation_stays_safe(pattern):
    """对照组：只有外层真叠了无界量词才拦；单层（外层无重复）仍放行。"""
    assert not is_dangerous_pattern(pattern)


# ── S3-R2：越界重复数不得逃出 compile_extra_rules（C2） ───────────────


def test_s3r2_overflow_repeat_count_is_dropped_as_invalid():
    """越界重复数让 re.compile 抛 OverflowError（非 re.error 子类）。

    必须按"非法正则"就地丢弃：不加入 rules、计入 dropped、产出一条 warning，
    绝不允许异常逃出（原实现只捕 re.error，异常会打掉调用方整条链路）。
    """
    raw = "(a){4294967296,}\n\\d{3}-\\d{4}"
    result = compile_extra_rules(raw)

    assert result.dropped == ((1, "invalid"),), "越界规则未按 invalid 记录"
    assert len(result.rules) == 1, "同配置里的合法规则被连累丢弃"
    assert len(result.warnings) == 1
    assert "无效的脱敏正则" in result.warnings[0]


def test_s3r2_pathological_nesting_does_not_escape_compile():
    """超深嵌套会让解析器抛 RecursionError（非 re.error 子类），同样必须就地丢弃。

    3000 层括号在默认递归上限下实测触发 RecursionError；若环境把递归上限调得
    极高而侥幸编译成功，也不得抛异常。断言两种情况之一的合法结果。
    """
    result = compile_extra_rules("(" * 3000 + "a" + ")" * 3000)

    if result.dropped:
        assert result.dropped == ((1, "invalid"),)
        assert result.rules == ()
    else:
        assert len(result.rules) == 1


def test_s3r2_overflow_rule_does_not_break_runtime_redact(monkeypatch):
    """端到端：越界配置下 redact() 不抛异常，且同配置的合法规则仍生效。"""
    from app.config import settings
    from app.runtime.core import redaction

    monkeypatch.setattr(settings, "redaction_enabled", True)
    monkeypatch.setattr(
        settings, "redaction_extra_patterns", "(a){4294967296,}\nsk-(live|test)+"
    )
    monkeypatch.setattr(redaction, "_extra_cache", None)
    monkeypatch.setattr(redaction, "_extra_signature", None)

    out = redaction.redact("token sk-live-12345")

    assert isinstance(out, str)
    assert "sk-live" not in out, "同配置中的合法规则未生效"
    assert "***" in out
    assert len(redaction._extra_cache) == 1


# ── S3-R2：verbose 模式 fail-closed 现状锁定（C5） ─────────────────────


@pytest.mark.parametrize("pattern", [r"(?x)(a|b)+", r"(?x)(ab)+  # a|b"])
def test_s3r2_verbose_mode_is_fail_closed(pattern):
    """verbose 模式下无法靠原文扫描证明安全，一律按危险处理。

    典型误伤：`(?x)(a|b)+` 实际可证明安全；`(?x)(ab)+  # a|b` 的 `|` 只出现在
    注释里也触发 blanket。方向是 fail-closed（宁可多拦不可漏拦），属有意取舍，
    本用例仅锁定现状，防止未来改动悄悄削弱。
    """
    assert is_dangerous_pattern(pattern)


# ── S3-R3：嵌套检查的 fail-closed 窄误伤与 C4(a) 边界 ────────────────


@pytest.mark.parametrize(
    "pattern",
    [r"((a|b)+x)+", r"((cat|dog)+!)+", r"((red|blue)+#)+"],
)
def test_s3r3_deterministic_separator_narrow_false_positive_is_locked(pattern):
    """实际安全但有意拦截，理由见 pattern_guard 注释。

    内层互斥字面分支后跟确定性分隔符，外层再重复时实际不会产生灾难回溯；
    嵌套无界组检查仍按 fail-closed 取舍拦截。
    """
    assert is_dangerous_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"(a+){2}", r"(a+){2,3}"])
def test_s3r3_bounded_outer_repeat_stays_safe(pattern):
    """C4(a) 的有界外层重复边界：有限重复不作为无界嵌套量词拦截。"""
    assert not is_dangerous_pattern(pattern)

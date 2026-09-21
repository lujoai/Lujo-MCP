"""额外脱敏正则的共享守卫 —— 无状态纯函数工具层。

U05-QDRANT-FIX：`settings.redaction_extra_patterns` 有两个消费方
（app/runtime/core/redaction.py 与 app/rag/qdrant_vector_store.py 的内联副本，
架构冻结禁止 rag→runtime import 故历史上复制了一份）。两份副本曾发生语义漂移
（v0.7.1-b10-2 副本漏应用配置），本次检测逻辑上线后再次漂移（副本无危险检测）。
本模块把「危险形态判定 + 编译过滤」收敛到 runtime / rag 都允许依赖的中立层，
两个消费方各自保留缓存与配置签名语义。

纯函数契约：不读 settings、不打日志、不持状态；warning 以已格式化的
字符串返回，由调用方经各自 logger 输出。

检测采用保守判定（fail-closed，无法证明安全时一律拦截）：

- 嵌套量词：组内量词 + 组外 `*` / `+` / 无界 `{n,}`（规则①，覆盖 `(a+){2,}`）；
- 嵌套无界重复：组紧邻无界量词、且其内部还有另一个紧邻无界量词的子组时直接
  拦截（如 `((a|b)+)+`——内层单独可证明互斥，但内外两层重复使每次迭代长度
  可变，产生指数级分段歧义；S3 因逐组孤立分析曾放行，属行为回归）；
- 重复分组含顶层交替时，只有能证明各分支是互不为前缀的纯字面量，才放行；
- verbose 模式（`(?x)`）含 `|` 与无界量词时一律拦截：注释里的括号/竖线无法靠
  原文扫描与真实结构区分，宁可误伤 `(?x)(a|b)+` 也不漏判，属有意取舍。

已知未覆盖形态（形态法保守检测的既有接受项，触发前提是作者自行配置，P2 非
远程 P1）：如 `(\\d{1,3})+`（内层有界量词 + 外层无界重复）的分段歧义、更深层
的组合嵌套等。本模块不追求完备判定，但保证「无法证明安全时不放行」。
「无法证明安全时不放行」仅指已进入分析但未能证明互斥的形态；已知未覆盖形态未进入分析，仍会放行。

已知取舍（fail-closed 误伤）：`((a|b)+x)+` 的内层互斥分支带确定性分隔符，
实际安全仍会被嵌套无界组规则拦截，属有意取舍。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

_SENSITIVE_KEY_NAME = (
    r"[\w.-]*(?:password|passwd|pwd|secret|token|apikey|credential|private[_-]?key)[\w.-]*"
    r"|[\w.-]*[_-]key"
)

DEFAULT_REDACT_RULES: tuple[tuple[str, str], ...] = (
    (
        r"(?i)\b(" + _SENSITIVE_KEY_NAME + r")\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|\S+)",
        r'\1="***"',
    ),
    (
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+))(?:'[^']*'|\"[^\"]*\"|\S+)",
        r"\1***",
    ),
    (
        r"(?i)\"(" + _SENSITIVE_KEY_NAME + r"|authorization)\"\s*:\s*(?:'[^']*'|\"[^\"]*\"|\S+)",
        r'"\1":"***"',
    ),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "***PHONE***"),
)

_COMPILED_DEFAULT_REDACT_RULES = tuple(
    (re.compile(pattern), replacement)
    for pattern, replacement in DEFAULT_REDACT_RULES
)

# W9 / P3-SEC-3：非法正则告警里模式原文的可见前缀长度。规则来自运维者自己的
# 配置、告警也写回运维者自己的日志，看似无泄露；但「规则里直接写了字面密钥」
# 时就形成自泄漏面（与同模块对危险回溯正则只报行号+长度是同一动机）。保留
# 一段前缀是为了可诊断——能认出是哪一条规则写错了。
_INVALID_PATTERN_LOG_CHARS = 32


def _pattern_for_log(pattern: str) -> str:
    """按 :data:`_INVALID_PATTERN_LOG_CHARS` 截断模式原文（只用于告警文本）。"""
    if len(pattern) <= _INVALID_PATTERN_LOG_CHARS:
        return pattern
    return pattern[:_INVALID_PATTERN_LOG_CHARS] + "…"


def contains_unredacted_secret(value: object) -> bool:
    """按内置规则的不动点判据检测字符串叶子，不修改传入内容。

    ⚠️ **已知边界（W9 / P4-安全，裁定见 CODE_REVIEW §0.6.2 第 5 条：不扩）**：
    判据只覆盖 :data:`DEFAULT_REDACT_RULES` 那 4 条内置规则，因此检出面**窄于**
    脱敏能力——AKIA / ghp_ / xoxb / eyJ 这类形态若未被上游脱敏，本函数不会拦。
    这是有意的：KB 边界现在是「不动点检测 + fail-closed 拒写」，扩宽内置规则
    会**直接扩大拒写面**，一条宽泛规则就可能把种子条目或正常经验误拒。要加形态
    只能走 ``redaction_extra_patterns`` 用户配置，且加完必须重跑误拒验收门
    （45 条种子 + KB 单测全部内容，检出数须为 0）。
    """
    if isinstance(value, str):
        return any(
            pattern.sub(replacement, value) != value
            for pattern, replacement in _COMPILED_DEFAULT_REDACT_RULES
        )
    if isinstance(value, dict):
        return any(contains_unredacted_secret(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_unredacted_secret(item) for item in value)
    return False

_DANGEROUS_REPEAT_RE = re.compile(
    # 嵌套量词：组内量词 + 组外无界量词 —— (a+)+ / (a*)* / (\w+)+ / (a+){2,}
    # S3-R2 C4(a)：量词部分从 [*+] 扩为 (?:[*+]|\{\d+,\})，覆盖 (a+){2,} 类
    # 形如「内层无界 + 外层无界 {n,}」的灾难形态；有界 {n} / {n,m} 不匹配。
    r"\([^()]*[*+][^()]*\)(?:[*+]|\{\d+,\})"
)
_UNBOUNDED_BRACE_RE = re.compile(r"\{\d+,\}")
_INLINE_FLAGS_RE = re.compile(r"\(\?[aiLmsux-]+(?::|\))")
_VERBOSE_FLAG_RE = re.compile(r"\(\?[aiLmsux-]*x[aiLmsux-]*(?::|\))")
_PURE_LITERAL_META = frozenset(r".*+?[]()|{}^$\\")


@dataclass(frozen=True)
class GuardResult:
    """compile_extra_rules 的返回值。

    rules: 编译成功且通过危险形态检测的 (正则, 替换串) 列表。
    warnings: 已格式化的 warning 文案，调用方逐条 logger.warning 输出。
        危险正则只报行号与长度（不泄露模式内容）；非法正则报行号、长度与
        **截断后的**模式前缀（W9 / P3-SEC-3：整条回显会在「规则里写了字面
        密钥」时形成自泄漏面），skip 降级语义不变。
    dropped: 被丢弃规则的 (行号, 原因码) 元组，原因码为 "dangerous" / "invalid"。
        供消费方在逐条 warning 之外输出「哪些规则未生效」的汇总；默认空元组，
        兼容 GuardResult(rules, warnings) 的旧构造方式。
    """

    rules: tuple[tuple[re.Pattern[str], str], ...]
    warnings: tuple[str, ...]
    dropped: tuple[tuple[int, str], ...] = ()


def is_dangerous_pattern(pattern: str) -> bool:
    """检测危险回溯形态；无法证明重复交替互斥时按危险处理。"""
    if _DANGEROUS_REPEAT_RE.search(pattern) is not None:
        return True

    # verbose 注释中的括号和竖线不是正则结构；遇到该模式时无法靠原文扫描证明安全。
    # 误伤方向为 fail-closed（如 (?x)(a|b)+ 实际安全也会被拦），属有意取舍。
    if _VERBOSE_FLAG_RE.search(pattern) and "|" in pattern and _has_unbounded_quantifier(pattern):
        return True

    has_inline_flags = _INLINE_FLAGS_RE.search(pattern) is not None
    spans = _group_spans(pattern)
    for opening, closing in spans:
        if not _has_unbounded_quantifier_at(pattern, closing + 1):
            continue
        # S3-R2 C1：内层「可证明互斥」的重复组被外层无界量词再次重复时
        # （((a|b)+)+），逐组孤立分析会漏判；嵌套检查必须先于互斥证明。
        # 若先因外层只有一个顶层分支而按 len(branches) < 2 continue，就会绕过该检查。
        # 代价是 ((a|b)+x)+ 这类内层互斥且带确定性分隔符、实际安全的形态也会被拦。
        # 方向为 fail-closed（宁可多拦不漏拦），属有意取舍，勿改为放宽。
        if _has_nested_unbounded_group(pattern, spans, opening, closing):
            return True
        body_start = _group_body_start(pattern, opening, closing)
        branches = _split_top_level_branches(pattern, body_start, closing)
        if len(branches) < 2:
            continue
        # 内联标志会让源码不同的字面量匹配同一文本，例如 (?i)(a|A)+。
        if has_inline_flags or not _branches_are_provably_disjoint(branches):
            return True
    return False


def _has_nested_unbounded_group(
    pattern: str, spans: list[tuple[int, int]], opening: int, closing: int
) -> bool:
    """组的 span 内部是否还有另一个紧邻无界量词的子组（真包含判定）。

    span 由括号栈配对得出，天然良构嵌套（无交叉），因此「真包含」只需
    ``inner_open > opening`` 且 ``inner_close < closing``。子组自身的无界
    量词要么落在外层 span 内（``inner_close + 1 < closing``），要么恰好是
    外层 ``)`` 本身（``inner_close + 1 == closing``，该字符不可能是量词），
    故无需额外边界判断；转义与字符类已由 _group_spans 内的跳过逻辑排除。

    动机：``(a|b)+`` 单独安全的前提是「每次迭代固定吞 1 字符」；被外层无界
    量词再重复后（``((a|b)+)+``），内层每次可吞任意长度 run，与外层重复叠加
    产生指数级分段歧义，故一律 fail-closed。
    """
    return any(
        inner_open > opening
        and inner_close < closing
        and _has_unbounded_quantifier_at(pattern, inner_close + 1)
        for inner_open, inner_close in spans
    )


def _has_unbounded_quantifier(pattern: str) -> bool:
    """粗略识别无界量词，供 verbose 模式下的 fail-closed 分支使用。"""
    return any(char in pattern for char in "*+") or _UNBOUNDED_BRACE_RE.search(pattern) is not None


def _has_unbounded_quantifier_at(pattern: str, index: int) -> bool:
    """识别分组紧邻的无界重复符；{3} 与 {1,3} 不在此列。"""
    return index < len(pattern) and pattern[index] in "*+" or _UNBOUNDED_BRACE_RE.match(pattern, index) is not None


def _skip_character_class(pattern: str, start: int, end: int) -> int:
    """跳过字符类，避免其中的括号和竖线改变分组结构。"""
    index = start + 1
    first_item = True
    while index < end:
        char = pattern[index]
        if char == "\\":
            index += 2
            first_item = False
            continue
        if char == "^" and first_item:
            index += 1
            continue
        if char == "]":
            if first_item:
                first_item = False
                index += 1
                continue
            return index + 1
        first_item = False
        index += 1
    return end


def _skip_comment_group(pattern: str, start: int, end: int) -> int:
    """跳过 (?#...) 注释，避免注释文本里的括号进入分组栈。"""
    close = pattern.find(")", start + 3, end)
    return end if close < 0 else close + 1


def _group_spans(pattern: str) -> list[tuple[int, int]]:
    """返回匹配的括号范围；转义、字符类与正则注释不参与配对。"""
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if pattern.startswith("(?#", index):
            index = _skip_comment_group(pattern, index, len(pattern))
            continue
        if char == "[":
            index = _skip_character_class(pattern, index, len(pattern))
            continue
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            spans.append((stack.pop(), index))
        index += 1
    return spans


def _group_body_start(pattern: str, opening: int, closing: int) -> int:
    """跳过允许作为纯字面分支容器的分组声明前缀。"""
    if pattern.startswith("(?:", opening):
        return opening + 3
    if pattern.startswith("(?P<", opening):
        name_end = pattern.find(">", opening + 4, closing)
        if name_end >= 0:
            return name_end + 1
    return opening + 1


def _split_top_level_branches(pattern: str, start: int, end: int) -> list[str]:
    """按当前分组深度切分交替，忽略转义、嵌套组、字符类和注释。"""
    branches: list[str] = []
    branch_start = start
    depth = 0
    index = start
    while index < end:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if pattern.startswith("(?#", index):
            index = min(_skip_comment_group(pattern, index, end), end)
            continue
        if char == "[":
            index = _skip_character_class(pattern, index, end)
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "|" and depth == 0:
            branches.append(pattern[branch_start:index])
            branch_start = index + 1
        index += 1
    branches.append(pattern[branch_start:end])
    return branches


def _branches_are_provably_disjoint(branches: list[str]) -> bool:
    """只用纯字面分支的前缀无关性证明分支语言不相交。"""
    if any(not branch or any(char in _PURE_LITERAL_META for char in branch) for branch in branches):
        return False

    # 首字符不同只是充分条件；契约中的 Bearer/Basic 共用首字母但仍是互不为前缀的字面量。
    return all(
        not left.startswith(right) and not right.startswith(left)
        for index, left in enumerate(branches)
        for right in branches[index + 1 :]
    )


def compile_extra_rules(
    raw: Union[str, None], replacement: str = "***"
) -> GuardResult:
    """逐行编译换行分隔的额外脱敏正则，过滤危险形态与非法正则。

    空行跳过；危险与非法正则均不阻断其余规则编译（warning + skip 降级语义，
    与两侧消费方既有契约一致）。
    """
    rules: list[tuple[re.Pattern[str], str]] = []
    warnings: list[str] = []
    dropped: list[tuple[int, str]] = []
    for index, line in enumerate((raw or "").splitlines(), 1):
        pattern = line.strip()
        if not pattern:
            continue
        if is_dangerous_pattern(pattern):
            dropped.append((index, "dangerous"))
            # 安全摘要：只报行号与长度，不输出模式内容——避免把作者可能
            # 用于匹配敏感数据的模式写进日志。
            warnings.append(
                f"跳过存在灾难性回溯风险的脱敏正则（第 {index} 行，"
                f"长度 {len(pattern)}）：嵌套量词/重叠重复结构在长输入上会"
                "指数级回溯，请改写为单层量词或非重叠交替形式"
            )
            continue
        try:
            rules.append((re.compile(pattern), replacement))
        except (re.error, OverflowError, RecursionError) as e:
            # S3-R2 C2：re.compile 还会抛非 re.error 的异常——越界重复数
            # （OverflowError，如 (a){4294967296,}）与超深嵌套（RecursionError，
            # 解析器递归超限）。二者都必须按非法正则就地丢弃：异常逃出会打掉
            # 调用方整条链路（redact / RedactingFormatter / errors.record 均无
            # 兜底，等于一条配置让全部脱敏与错误记录崩溃）。不改为裸
            # except Exception，避免吞掉编程错误。
            dropped.append((index, "invalid"))
            # W9 / P3-SEC-3：原文按前缀截断 + 报行号与长度（不再整条回显）
            warnings.append(
                f"跳过无效的脱敏正则（第 {index} 行，长度 {len(pattern)}）"
                f"{_pattern_for_log(pattern)!r}: {e}"
            )
            continue
    return GuardResult(rules=tuple(rules), warnings=tuple(warnings), dropped=tuple(dropped))

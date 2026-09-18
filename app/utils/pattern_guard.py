"""额外脱敏正则的共享守卫 —— 无状态纯函数工具层。

U05-QDRANT-FIX：`settings.redaction_extra_patterns` 有两个消费方
（app/runtime/core/redaction.py 与 app/rag/qdrant_vector_store.py 的内联副本，
架构冻结禁止 rag→runtime import 故历史上复制了一份）。两份副本曾发生语义漂移
（v0.7.1-b10-2 副本漏应用配置），本次检测逻辑上线后再次漂移（副本无危险检测）。
本模块把「危险形态判定 + 编译过滤」收敛到 runtime / rag 都允许依赖的中立层，
两个消费方各自保留缓存与配置签名语义。

纯函数契约：不读 settings、不打日志、不持状态；warning 以已格式化的
字符串返回，由调用方经各自 logger 输出。

检测为保守形态法（宁可漏拦不误伤）：只拦「组内含量词 + 组外再叠量词」的
嵌套量词与「组内交替 + 组外量词」的重叠重复两类明确结构，形态外的危险正则
仍可能放行，属已知接受项（触发前提是作者自行配置，P2 非远程 P1）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

_DANGEROUS_REPEAT_RE = re.compile(
    r"\([^()]*[*+][^()]*\)[*+]"      # 组内量词 + 组外量词（嵌套量词）：(a+)+ / (a*)* / (\w+)+
    r"|\([^()|]*\|[^()|]*\)[*+]"     # 组内交替 + 组外量词（重叠重复）：(a|a)*
)


@dataclass(frozen=True)
class GuardResult:
    """compile_extra_rules 的返回值。

    rules: 编译成功且通过危险形态检测的 (正则, 替换串) 列表。
    warnings: 已格式化的 warning 文案，调用方逐条 logger.warning 输出。
        危险正则只报行号与长度（不泄露模式内容）；非法正则保持既有
        「warning 含模式原文 + skip」行为不变。
    """

    rules: tuple[tuple[re.Pattern[str], str], ...]
    warnings: tuple[str, ...]


def is_dangerous_pattern(pattern: str) -> bool:
    """检测正则是否含已确认的灾难性回溯形态（嵌套量词 / 重叠重复）。"""
    return _DANGEROUS_REPEAT_RE.search(pattern) is not None


def compile_extra_rules(
    raw: Union[str, None], replacement: str = "***"
) -> GuardResult:
    """逐行编译换行分隔的额外脱敏正则，过滤危险形态与非法正则。

    空行跳过；危险与非法正则均不阻断其余规则编译（warning + skip 降级语义，
    与两侧消费方既有契约一致）。
    """
    rules: list[tuple[re.Pattern[str], str]] = []
    warnings: list[str] = []
    for index, line in enumerate((raw or "").splitlines(), 1):
        pattern = line.strip()
        if not pattern:
            continue
        if is_dangerous_pattern(pattern):
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
        except re.error as e:
            warnings.append(f"跳过无效的脱敏正则 {pattern!r}: {e}")
            continue
    return GuardResult(rules=tuple(rules), warnings=tuple(warnings))

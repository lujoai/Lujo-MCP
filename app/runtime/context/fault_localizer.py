"""FaultLocalizer —— 从 stack frames 中筛选"最值得 AI Agent 优先检查的位置"。

设计定位（V1，启发式规则）：
- 目标不是"自动确定绝对根因（absolute root cause）"，
  而是从大量栈帧中筛选出最可疑、最值得优先检查的候选位置。
- 每个打分都可解释：输出命中的规则、加分与排序原因，供 AI 直接消费。
- 纯计算模块：无数据库访问、无文件写入、无 LLM/RAG/Agent 调用。

依赖约束：
- 仅允许 app.runtime.context（自身）、app.runtime.collectors.static_analyzer
- 以及 Python 标准库。

架构约束（ARCHITECTURE_REVIEW_V1）：
- 归属 runtime 层，作为 Debug Context 生成能力的一部分；
- 不依赖 app.mcp / app.agent / app.llm / app.rag。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from app.runtime.collectors import static_analyzer

logger = logging.getLogger("lujo-mcp.runtime.context.fault_localizer")

# ── 评分权重（V1 启发式初值，可调） ──
_W_STACK_POSITION = 20      # 栈位置：靠近异常抛出点 / 靠近被调深层
_W_SUSPICIOUS_INPUT = 25    # static_analyzer 命中可疑输入（单条 +12，封顶 25）
_W_COMPLEXITY = 20          # 复杂度提示命中
_W_PROJECT_CODE = 15        # 是否项目代码
_W_CALL_CHAIN = 10          # 调用链汇聚点

# 项目代码识别：路径特征排除（stdlib / 三方包 / 虚拟环境）
_IGNORED_PATH_PARTS = (
    "site-packages",
    "dist-packages",
    "node_modules",
    ".venv",
    "venv",
    "virtualenv",
    "lib/python",
    "Lib/site-packages",
)
# Python 标准库根目录（真实路径，避免误判 sys 前缀）
_STDLIB_DIRS: tuple[str, ...] = tuple(
    str(p).replace("\\", "/") for p in getattr(sys, "path", []) if p
)


@dataclass(slots=True)
class ScoreContribution:
    """单个加分项的来源说明 —— 保证评分可解释。"""

    rule: str          # 命中的规则名，如 "stack_position"
    points: int        # 加分值
    reason: str        # 人类可读的解释


@dataclass(slots=True)
class SuspiciousFrame:
    """单个候选可疑帧（V1 输出）。"""

    file: str
    function: str
    line: int
    score: float                   # 0-100，越高越可疑
    reasons: list[str] = field(default_factory=list)
    is_likely_cause: bool = False  # 是否最可能根因候选
    is_project_code: bool = False  # 是否项目代码（非 stdlib/三方）
    contributions: list[ScoreContribution] = field(default_factory=list)


@dataclass(slots=True)
class FaultLocalizationResult:
    """故障定位结果 —— 只筛选"优先检查候选"，不声称绝对根因。"""

    suspicious_frames: list[SuspiciousFrame] = field(default_factory=list)
    likely_cause_candidate: Optional[str] = None  # 一句话最值得优先检查的位置
    method: str = "heuristic_stack_score"
    # 排序规则说明，供 AI 解释
    sort_explanation: str = (
        "按启发式规则加权打分降序：栈位置(30) + 可疑输入(25) + 复杂度(20) + "
        "项目代码(15) + 调用链汇聚(10)。分数越高越值得优先检查，不代表绝对根因。"
    )


def _is_project_code(file_path: str) -> bool:
    """判断帧是否属于项目代码（排除 stdlib / 三方包 / 虚拟环境路径）。"""
    if not file_path:
        return False
    p = str(file_path).replace("\\", "/")
    for part in _IGNORED_PATH_PARTS:
        if part in p:
            return False
    # 匹配标准库路径前缀（如 C:/Python312/lib/...）
    for std in _STDLIB_DIRS:
        if std and (p == std or p.startswith(std + "/")):
            return False
    # 常见 stdlib 模块形态（以 `<frozen>` 等非文件路径）
    if p.startswith("<") and p.endswith(">"):
        return False
    return True


def _is_builtin_or_frozen(file_path: str) -> bool:
    """判定 <built-in> / <frozen> 等特殊帧（无源码，直接降权）。"""
    p = str(file_path or "")
    return p.startswith("<") and p.endswith(">")


def _stack_position_score(index: int, total: int) -> tuple[int, str]:
    """栈位置信号：靠近异常抛出点（栈顶）的帧更接近错误现场。

    归一化：index 越小（越靠栈顶）分越高。
    """
    if total <= 0:
        return 0, "no frames"
    # 线性映射：栈顶 30 分，栈底趋近 0（但保留至少 1 分区分）
    raw = max(1, int(round((1 - index / total) * _W_STACK_POSITION)))
    return raw, f"stack position: frame {index + 1}/{total}, closer to exception site"


def _call_chain_score(fault: Optional[static_analyzer.FaultLocation]) -> tuple[int, str]:
    """调用链信号：处于调用链汇聚点的帧更可能承接问题传播。"""
    if fault is None or not fault.call_chain:
        return 0, "no call chain signal"
    return _W_CALL_CHAIN, f"call chain hub: participates in {len(fault.call_chain)} calls"


def _suspicious_input_score(
    fault: Optional[static_analyzer.FaultLocation],
) -> tuple[int, str, list[str]]:
    """可疑输入信号：命中 static_analyzer 的可疑输入 → 显著加分并收集原因。"""
    if fault is None or not fault.suspicious_inputs:
        return 0, "", []
    reasons = [
        f"suspicious input: {si.get('variable', '?')} — {si.get('reason', '')}"
        for si in fault.suspicious_inputs
    ]
    # 每条可疑输入单独加分（单条 +12，封顶 25）
    pts = min(_W_SUSPICIOUS_INPUT, len(fault.suspicious_inputs) * 12)
    return pts, f"{len(fault.suspicious_inputs)} suspicious input(s)", reasons


def _complexity_score(fault: Optional[static_analyzer.FaultLocation]) -> tuple[int, str, list[str]]:
    """复杂度信号：高嵌套 / 长函数 / 多分支提示。"""
    if fault is None or not fault.function_info:
        return 0, "", []
    hints = fault.function_info.complexity_hints
    if not hints:
        return 0, "", []
    pts = min(_W_COMPLEXITY, len(hints) * 8)
    return pts, f"{len(hints)} complexity hint(s)", list(hints)


def _project_code_score(is_project: bool) -> tuple[int, str]:
    """项目代码信号：项目内帧更值得检查。"""
    if is_project:
        return _W_PROJECT_CODE, "project code (non-stdlib, non-vendor)"
    return 0, "stdlib/vendor frame (low priority)"


def _exception_rule_boost(
    exc_type: Optional[str], frame: dict[str, Any], fault: Optional[static_analyzer.FaultLocation]
) -> tuple[int, str]:
    """异常类型/消息规则：针对常见异常给特定帧加分（V1 最小集）。"""
    et = (exc_type or "").lower()
    if not et:
        return 0, ""
    # KeyError → 倾向 dict.get() 未校验 / 索引访问帧（suspicious_inputs 已覆盖，此处再补强）
    if "keyerror" in et:
        fn = (frame.get("function") or "").lower()
        if "get" in fn or fault is not None and any(
            "index" in si.get("reason", "").lower() for si in fault.suspicious_inputs
        ):
            return 8, f"KeyError: {frame.get('function')} likely unvalidated key access"
    # TypeError → 倾向栈顶（解引用/参数类型）帧
    if "typeerror" in et and fault is not None:
        return 8, "TypeError: type mismatch likely at this frame"
    return 0, ""


def localize(
    frames: list[dict[str, Any]],
    exc_type: Optional[str] = None,
    message: Optional[str] = None,
    max_candidates: int = 8,
) -> FaultLocalizationResult:
    """对栈帧列表做可疑度打分排序，返回优先检查候选（V1）。

    - 纯计算，无 IO / DB / LLM / RAG / Agent。
    - 单帧分析失败或帧缺失字段 → 静默降级，不抛异常。
    - frames 为空 → 空结果。
    """
    if not frames:
        return FaultLocalizationResult()

    try:
        static_results = static_analyzer.analyze(frames)
    except Exception as exc:  # 静默降级：静态分析失败不阻断定位
        logger.warning("static_analyzer.analyze failed, degrade to position-only: %s", exc)
        static_results = []

    # 建立 帧→FaultLocation 映射。
    # FIX: P1-9a 必须按 FaultLocation.frame_index 关联原始帧（analyze 会跳过部分帧，
    # 结果列表下标 ≠ 输入 frames 下标），否则张冠李戴。
    static_by_index: dict[int, Optional[static_analyzer.FaultLocation]] = {}
    for i, fl in enumerate(static_results):
        if fl.frame_index is not None:
            static_by_index[fl.frame_index] = fl
        else:
            # 兼容旧调用方/mock（未设置 frame_index 时回退顺序映射）
            static_by_index[i] = fl

    total = len(frames)
    candidates: list[SuspiciousFrame] = []

    for idx, frame in enumerate(frames):
        file_path = str(frame.get("file") or "")
        line = int(frame.get("line") or 0)
        function = str(frame.get("function") or "<unknown>")

        fault = static_by_index.get(idx)

        # 1) 栈位置
        pos_pts, pos_reason = _stack_position_score(idx, total)
        # 2) 调用链
        cc_pts, cc_reason = _call_chain_score(fault)
        # 3) 可疑输入
        si_pts, si_reason, si_reasons = _suspicious_input_score(fault)
        # 4) 复杂度
        cx_pts, cx_reason, cx_hints = _complexity_score(fault)
        # 5) 项目代码
        is_project = _is_project_code(file_path)
        proj_pts, proj_reason = _project_code_score(is_project)
        # 6) 异常规则
        ex_pts, ex_reason = _exception_rule_boost(exc_type, frame, fault)

        # 特殊帧（<built-in>/<frozen>）直接压制为低分
        if _is_builtin_or_frozen(file_path):
            is_project = False
            proj_pts = 0
            pos_pts = max(1, pos_pts // 3)

        score = float(pos_pts + si_pts + cx_pts + proj_pts + cc_pts + ex_pts)
        score = min(100.0, score)

        contributions: list[ScoreContribution] = []
        reasons: list[str] = []
        for rule, pts, reason in (
            ("stack_position", pos_pts, pos_reason),
            ("suspicious_input", si_pts, si_reason),
            ("complexity", cx_pts, cx_reason),
            ("project_code", proj_pts, proj_reason),
            ("call_chain", cc_pts, cc_reason),
            ("exception_rule", ex_pts, ex_reason),
        ):
            if pts > 0:
                contributions.append(ScoreContribution(rule=rule, points=pts, reason=reason))
                reasons.append(f"{rule}(+{pts}): {reason}")
        reasons.extend(si_reasons)
        reasons.extend(cx_hints)

        candidates.append(
            SuspiciousFrame(
                file=file_path,
                function=function,
                line=line,
                score=score,
                reasons=reasons,
                is_project_code=is_project,
                contributions=contributions,
            )
        )

    # 排序：score 降序；同分时项目代码帧优先、证据（贡献项）更多者优先
    candidates.sort(
        key=lambda f: (
            f.score,
            1 if f.is_project_code else 0,
            len(f.contributions),
        ),
        reverse=True,
    )

    # 标记 likely_cause_candidate：最高分且为项目代码的帧；否则取最高分帧兜底
    result = FaultLocalizationResult(suspicious_frames=candidates[:max_candidates])
    if candidates:
        top_project = next((f for f in candidates if f.is_project_code), candidates[0])
        top_project.is_likely_cause = True
        result.likely_cause_candidate = (
            f"{top_project.file}:{top_project.line} in {top_project.function} "
            f"(score={top_project.score:.0f})"
        )
    return result


# 供 builder 注入的 dict 化辅助（公开接口，纯数据转换）
def to_payload(result: FaultLocalizationResult) -> dict[str, Any]:
    """将定位结果转成可注入 DebugContextPayload 的 dict（可选字段，纯数据）。"""
    return {
        "method": result.method,
        "sort_explanation": result.sort_explanation,
        "likely_cause_candidate": result.likely_cause_candidate,
        "suspicious_frames": [
            {
                "file": f.file,
                "function": f.function,
                "line": f.line,
                "score": f.score,
                "is_likely_cause": f.is_likely_cause,
                "is_project_code": f.is_project_code,
                "reasons": f.reasons,
                "contributions": [
                    {"rule": c.rule, "points": c.points, "reason": c.reason}
                    for c in f.contributions
                ],
            }
            for f in result.suspicious_frames
        ],
    }


__all__ = [
    "FaultLocalizationResult",
    "ScoreContribution",
    "SuspiciousFrame",
    "localize",
    "to_payload",
    "_is_project_code",
]

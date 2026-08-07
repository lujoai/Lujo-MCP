"""Debug Experience Record —— Retriever 输出 DTO/View（P1 Debug Experience RAG）。

定位约束（Architecture Frozen）：
- 只作为 `app/rag/retriever.py` 的输出视图，**不创建存储**、**不替代 DebugCase**。
- 数据来源：已有 DebugCase / KnowledgeBase / verify writeback 结果（只读映射）。
- 纯数据转换：无 IO、无数据库访问、无副作用。

设计原则：
- `from_kb_entry()`：把 KB entry（KnowledgeBaseStore 返回的 dict）映射为记录；
- `from_debug_context()`：从 `build_debug_context` 输出提取特征（供检索摘要/定位候选）；
- 可失败静默降级：字段缺失 → 空字符串 / 空列表 / 0.0，不抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.rag.debug_case import normalize_message_for_similarity


def _verification_text(verify_count: int, confidence: float) -> str:
    """从验证统计生成可读的 verification_result。"""
    try:
        count = int(verify_count or 0)
        conf = float(confidence or 0.0)
    except (TypeError, ValueError):
        return "unverified"
    if count > 0:
        return f"verified({count}次, confidence={conf:.2f})"
    return "unverified"


@dataclass(slots=True)
class DebugExperienceRecord:
    """一次可复用的调试经验（检索视图，非存储单元）。"""

    fingerprint: str = ""
    exception_type: str = ""
    message_pattern: str = ""
    debug_context_summary: str = ""
    fault_location: list[str] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)
    solution: str = ""
    verification_result: str = "unverified"
    confidence: float = 0.0
    # 检索来源标记（retriever 赋值）：fingerprint / message_similarity / vector
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 dict（供 Agent 上下文 JSON 序列化）。"""
        return {
            "fingerprint": self.fingerprint,
            "exception_type": self.exception_type,
            "message_pattern": self.message_pattern,
            "debug_context_summary": self.debug_context_summary,
            "fault_location": list(self.fault_location),
            "analysis": dict(self.analysis),
            "solution": self.solution,
            "verification_result": self.verification_result,
            "confidence": self.confidence,
            "source": self.source,
        }

    # ── 构造：从 KB entry 映射 ──

    @classmethod
    def from_kb_entry(cls, entry: Optional[dict[str, Any]]) -> "DebugExperienceRecord":
        """从 KB entry（KnowledgeBaseStore.get 返回的 dict）构建记录。

        兼容两种形态：
        - 顶层携带 fingerprint/fix_suggestion/case_confidence/verify_count；
        - analysis 内嵌 exception_type/message/source_files/fix_suggestion。
        字段缺失时静默降级为默认值。
        """
        if not entry:
            return cls()

        analysis = entry.get("analysis") or {}
        if not isinstance(analysis, dict):
            analysis = {}

        exc_type = str(analysis.get("exception_type") or "")
        message = str(analysis.get("message") or "")
        solution = str(
            entry.get("fix_suggestion")
            or analysis.get("fix_suggestion")
            or analysis.get("solution")
            or ""
        )

        source_files = analysis.get("source_files") or []
        fault_location = [str(f) for f in source_files if f] if isinstance(source_files, list) else []

        verify_count = entry.get("verify_count") or 0
        raw_conf = entry.get("case_confidence")
        if raw_conf is None:
            raw_conf = analysis.get("case_confidence") or 0.0
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = 0.0

        return cls(
            fingerprint=str(entry.get("fingerprint") or ""),
            exception_type=exc_type,
            message_pattern=normalize_message_for_similarity(message),
            fault_location=fault_location,
            analysis=dict(analysis),
            solution=solution,
            verification_result=_verification_text(verify_count, confidence),
            confidence=confidence,
        )

    # ── 构造：从 build_debug_context 输出提取特征 ──

    @classmethod
    def from_debug_context(
        cls, debug_context: Optional[dict[str, Any]]
    ) -> "DebugExperienceRecord":
        """从 build_debug_context 输出提取特征，生成当前调试经验快照。

        用于生成 debug_context_summary 与 fault_location（fault_localization 候选），
        供 retriever 做摘要/比对。solution/verification 未知时为默认值。
        """
        if not debug_context:
            return cls()

        exception = debug_context.get("exception") or {}
        if not isinstance(exception, dict):
            exception = {}

        exc_type = str(exception.get("type") or "")
        message = str(exception.get("message") or "")
        extra = debug_context.get("extra") or {}
        fingerprint = ""
        if isinstance(extra, dict):
            fingerprint = str(extra.get("fingerprint") or "")

        # fault_location：优先 fault_localization 候选帧，其次 static_analysis
        fault_location: list[str] = []
        fl = debug_context.get("fault_localization") or {}
        if isinstance(fl, dict):
            frames = fl.get("suspicious_frames") or []
            if isinstance(frames, list):
                seen: set[str] = set()
                for f in frames:
                    if not isinstance(f, dict):
                        continue
                    file_path = str(f.get("file") or "")
                    if file_path and file_path not in seen:
                        seen.add(file_path)
                        fault_location.append(file_path)
        static = debug_context.get("static_analysis") or {}
        if isinstance(static, dict) and static.get("file"):
            if static["file"] not in fault_location:
                fault_location.append(str(static["file"]))

        summary = _build_summary(exc_type, message, fault_location)

        return cls(
            fingerprint=fingerprint,
            exception_type=exc_type,
            message_pattern=normalize_message_for_similarity(message),
            debug_context_summary=summary,
            fault_location=fault_location,
            analysis=dict(exception),
        )


def _build_summary(exc_type: str, message: str, fault_location: list[str]) -> str:
    """生成 debug_context_summary（简短、可读）。"""
    parts: list[str] = []
    if exc_type:
        parts.append(f"type={exc_type}")
    if message:
        parts.append(f"message={message}")
    if fault_location:
        parts.append(f"location={','.join(fault_location[:3])}")
    return " | ".join(parts)

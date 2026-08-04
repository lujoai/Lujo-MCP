"""Debug Case 标准 Schema —— 异常调试案例的结构化记录与指纹计算。

v0.4.0 M2 引入。为知识库提供统一的分析记录模型，支撑三级 fallback 匹配：
- L1 精确指纹：完整异常指纹（含变量值）精确命中
- L1.5 归一化指纹：异常类型 + 归一化消息（去变量值/数字/地址）命中
- L2 类型级 Jaccard：归一化消息 token 重叠相似度兜底

设计原则：
- 纯函数、零外部依赖、无副作用
- 可失败静默降级（异常时不抛错，返回 None / 空 token）
- 与 KB entry 双向转换（to_kb_entry / from_kb_entry），幂等
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ── 归一化正则（用于剥离变量值噪声）──────────────────────────────
# 数字字面量（含负数、小数）
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# 十六进制对象地址（如 0x7f8a1b2c3d4e）
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
# UUID
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# 路径（/a/b/c 或 a\\b\\c）
_PATH_RE = re.compile(r"[/\\][\w.\-]+(?:[/\\][\w.\-]+)+")
# 带引号的字符串字面量（'abc' / "xyz"），视为变量值剥离
_QUOTED_RE = re.compile(r"['\"][^'\"]*['\"]")
# 连续空白
_WS_RE = re.compile(r"\s+")
# 类型名里的模块前缀与泛型参数（如 "builtins.ValueError"、"list[int]"）
_TYPE_MODULE_RE = re.compile(r"^[a-zA-Z_][\w.]*\.")
_TYPE_GENERIC_RE = re.compile(r"\[.*\]$")


def normalize_message_for_similarity(message: str) -> str:
    """归一化异常消息，剥离变量值噪声，返回可用于相似度匹配的文本。

    做法：转小写 → 去 hex 地址 → 去 UUID → 去路径 → 去数字 → 压缩空白。
    该函数对匹配语义负责：相同模式、不同变量值应归一为相同文本。
    """
    if not message:
        return ""
    text = _HEX_RE.sub(" ", message)
    text = _UUID_RE.sub(" ", text)
    text = _PATH_RE.sub(" ", text)
    text = _QUOTED_RE.sub(" ", text)
    text = _NUM_RE.sub(" ", text)
    text = text.lower()
    text = _WS_RE.sub(" ", text).strip()
    return text


def compute_type_fingerprint(exception_type: Optional[str]) -> str:
    """计算类型级指纹（异常类型名，去模块前缀与泛型参数）。

    例如："builtins.ValueError" → "valueerror"；"list[int]" → "list"。
    匹配语义：同类型异常视为一类，用于 L1.5 / L2 兜底。
    """
    if not exception_type:
        return ""
    t = _TYPE_GENERIC_RE.sub("", exception_type)
    t = _TYPE_MODULE_RE.sub("", t)
    return t.strip().lower()


def tokenize_for_similarity(message: str) -> set[str]:
    """将归一化消息切分为 token 集合（用于 Jaccard 相似度）。"""
    text = normalize_message_for_similarity(message)
    if not text:
        return set()
    return {tok for tok in text.split() if tok}


def compute_normalized_fingerprint(
    exception_type: Optional[str], message: str
) -> str:
    """计算归一化指纹 = 类型指纹 + 归一化消息（L1.5 匹配键）。

    相同模式、不同变量值 → 相同归一化指纹。
    """
    t = compute_type_fingerprint(exception_type)
    norm = normalize_message_for_similarity(message)
    if not t and not norm:
        return ""
    return f"{t}:{norm}".strip(":")


@dataclass(slots=True)
class DebugCase:
    """标准化的调试案例记录（知识库中一次可复用的分析结论）。"""

    exception_type: str
    message: str
    fingerprint: str
    root_cause: str
    fix_suggestion: str
    tags: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)
    # 证据置信度（0-1），M4 Verify Loop 会更新
    case_confidence: float = 0.0
    # 被验证通过的次数，M4 Verify Loop 会递增
    verify_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_kb_entry(self) -> dict[str, Any]:
        """转成 KnowledgeBaseStore.upsert 所需的 entry 格式。"""
        analysis = {
            "root_cause": self.root_cause,
            "fix_suggestion": self.fix_suggestion,
            "exception_type": self.exception_type,
            "message": self.message,
            "tags": list(self.tags),
            "source_files": list(self.source_files),
            "case_confidence": self.case_confidence,
            "verify_count": self.verify_count,
        }
        analysis.update(self.analysis)
        return {
            "fingerprint": self.fingerprint,
            "analysis": analysis,
            "fix_suggestion": self.fix_suggestion,
            "source": "debug_case",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_kb_entry(cls, entry: dict[str, Any]) -> "DebugCase":
        """从 KB entry 还原 DebugCase（_kb_meta 往返保留）。

        兼容两种形态：entry 的 analysis 含 exception_type/message，或 meta 字段。
        """
        analysis = entry.get("analysis") or {}
        meta = entry.get("_kb_meta") or {}

        exception_type = (
            meta.get("exception_type")
            or analysis.get("exception_type")
            or ""
        )
        message = meta.get("message") or analysis.get("message") or ""

        return cls(
            exception_type=exception_type,
            message=message,
            fingerprint=entry.get("fingerprint", ""),
            root_cause=analysis.get("root_cause", ""),
            fix_suggestion=entry.get("fix_suggestion", ""),
            tags=list(analysis.get("tags") or []),
            source_files=list(analysis.get("source_files") or []),
            analysis=analysis,
            case_confidence=float(analysis.get("case_confidence", 0.0) or 0.0),
            verify_count=int(analysis.get("verify_count", 0) or 0),
            created_at=float(meta.get("created_at", entry.get("created_at", 0.0)) or 0.0),
            updated_at=float(meta.get("updated_at", entry.get("updated_at", 0.0)) or 0.0),
        )
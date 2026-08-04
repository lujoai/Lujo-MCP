"""
敏感信息脱敏 —— 在数据进入存储 / 返回给 AI / 交给 LLM 前统一掩码。

设计要点（按 proj1 架构重新实现，非复制 proj2）：
- 纯函数 redact(text) -> str | None，对 None / 非字符串 / 空串原样返回。
- 默认覆盖常见密钥类字段：password / api_key / token / secret / Authorization / 手机号。
- 受 settings.redaction_enabled 控制，默认开启（fail-safe：宁可多掩也不泄露）。
- 额外正则由 settings.redaction_extra_patterns（换行分隔）提供，
  无效正则静默跳过不阻断主流程；编译结果按配置签名缓存，避免重复编译。
"""
import re
import logging
import threading
from typing import Optional

from app.config import settings

logger = logging.getLogger("ai-debug-mcp.redaction")

# 默认脱敏规则：(编译后的正则, 替换串)
_DEFAULT_RULES: list[tuple["re.Pattern[str]", str]] = [
    # password = "x", pwd: xxx, passwd='y'
    (
        re.compile(r"(?i)\b(password|pwd|passwd)\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|\S+)"),
        r'\1="***"',
    ),
    # api_key / api-key / token / secret / access_token / private_key = ...
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|token|access[_-]?token|auth[_-]?token|private[_-]?key)\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|\S+)"
        ),
        r'\1="***"',
    ),
    # Authorization: Bearer xxx
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+))(?:'[^']*'|\"[^\"]*\"|\S+)"),
        r"\1***",
    ),
    # JSON 格式: {"password":"xxx"}
    (
        re.compile(r"(?i)\"(password|pwd|passwd)\"\s*:\s*(?:'[^']*'|\"[^\"]*\"|\S+)"),
        r'"\1":"***"',
    ),
    # JSON 格式: {"api_key":"xxx"}, {"token":"xxx"}, {"secret":"xxx"}, {"authorization":"xxx"}
    (
        re.compile(
            r"(?i)\"(api[_-]?key|apikey|secret|token|access[_-]?token|auth[_-]?token|private[_-]?key|authorization)\"\s*:\s*(?:'[^']*'|\"[^\"]*\"|\S+)"
        ),
        r'"\1":"***"',
    ),
    # 中国大陆 11 位手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***PHONE***"),
]

# 额外规则缓存（按配置内容签名，配置变化时重建）
_extra_cache: Optional[list[tuple["re.Pattern[str]", str]]] = None
_extra_signature: Optional[str] = None
_extra_lock = threading.Lock()


def _load_extra_rules() -> list[tuple["re.Pattern[str]", str]]:
    """编译并缓存用户配置的额外正则；配置变化时重新编译。线程安全。"""
    global _extra_cache, _extra_signature
    # 快速路径：缓存命中
    if _extra_cache is not None and _extra_signature == (settings.redaction_extra_patterns or ""):
        return _extra_cache

    with _extra_lock:
        # double-check
        if _extra_cache is not None and _extra_signature == (settings.redaction_extra_patterns or ""):
            return _extra_cache

        raw = settings.redaction_extra_patterns or ""
        rules: list[tuple["re.Pattern[str]", str]] = []
        for line in raw.splitlines():
            pattern = line.strip()
            if not pattern:
                continue
            try:
                rules.append((re.compile(pattern), "***"))
            except re.error as e:
                logger.warning("跳过无效的脱敏正则 %r: %s", pattern, e)
                continue

        _extra_cache = rules
        _extra_signature = raw
        return rules


def redact(text: Optional[str]) -> Optional[str]:
    """对文本做脱敏。

    - None / 非字符串 / 空串：原样返回。
    - settings.redaction_enabled=False：原样返回。
    """
    if not isinstance(text, str) or not text:
        return text
    if not settings.redaction_enabled:
        logger.warning("redaction is disabled — sensitive data will NOT be masked")
        return text
    for pattern, repl in _DEFAULT_RULES:
        text = pattern.sub(repl, text)
    for pattern, repl in _load_extra_rules():
        text = pattern.sub(repl, text)
    return text

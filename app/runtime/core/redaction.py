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
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("lujo-mcp.redaction")

# 敏感键名模式（FIX: CR-2）：
# 此前用 \b(固定键名列表) 匹配，词边界在 '_' 处不成立（_ 是 word 字符），
# refresh_token / client_secret / session_token / api_secret 等下划线复合键
# 整体漏脱敏。改为"键名包含敏感词干"语义：
# - 词干：password / passwd / pwd / secret / token / apikey / credential /
#   private[_-]?key（覆盖 refresh_token、client_secret、my_secret_value 等）
# - 复合键后缀：[_-]key（覆盖 api_key / access_key / consumer_key / secret_key）
# 词干不包含裸 "key"（keyword / monkey 不误伤）与裸 "auth"（author 不误伤）。
_SENSITIVE_KEY_NAME = (
    r"[\w.-]*(?:password|passwd|pwd|secret|token|apikey|credential|private[_-]?key)[\w.-]*"
    r"|[\w.-]*[_-]key"
)

# 默认脱敏规则：(编译后的正则, 替换串)
_DEFAULT_RULES: list[tuple["re.Pattern[str]", str]] = [
    # password = "x", pwd: xxx, refresh_token=eyJ..., client_secret=xxx ...
    (
        re.compile(
            r"(?i)\b(" + _SENSITIVE_KEY_NAME + r")\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|\S+)"
        ),
        r'\1="***"',
    ),
    # Authorization: Bearer xxx
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+))(?:'[^']*'|\"[^\"]*\"|\S+)"),
        r"\1***",
    ),
    # JSON 格式: {"password":"xxx"}, {"refresh_token":"xxx"}, {"api_key":"xxx"} ...
    (
        re.compile(
            r"(?i)\"(" + _SENSITIVE_KEY_NAME + r"|authorization)\"\s*:\s*(?:'[^']*'|\"[^\"]*\"|\S+)"
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


# ── 结构化数据脱敏（dict/list 递归 + 键名白名单）────────────────────────────
# FIX: A2 —— 此前该逻辑内联在 trace_repo，logs.add_log 等直接写存储的路径
# 无法复用（trace_repo ↔ logs 存在循环 import），导致 POST /debug 的原始
# payload（可含 password/token 字段）明文入库。现统一下沉到本模块，
# 所有存储边界（trace_repo / logs / stacktrace / context_prep）共用一份实现。

# Phase 2：复合键名脱敏扩展
# 敏感子串集合：键名（小写）包含任一子串即视为敏感键，
# 覆盖 db_password / user_token / auth_header / secret_config 等复合键名。
_SENSITIVE_SUBSTRINGS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "key",
    "auth",
    "cookie",
}

# 内置白名单：含敏感子串但属于正常字段（不应脱敏）。
# password_hash=哈希后密码（非明文）、public_key=公钥（非私钥）、
# key_count/key_id/key_type=键数量/标识/类型（非密钥本身）、
# author*/authority=git blame 归因字段（FIX: R7-S2 —— 子串 "auth" 曾误伤
# author，"这行谁改的" 归因核心信息在送 LLM 前被整值掩码；注意
# authorization 不在白名单，仍按敏感头处理）。
_DEFAULT_ALLOWLIST = {
    "password_hash",
    "public_key",
    "key_count",
    "key_id",
    "key_type",
    "author",
    "author_time",
    "author_email",
    "author_mail",
    "authors",
    "authority",
}

# 白名单缓存（按配置签名，配置变化时重建）
_allowlist_cache: Optional[set[str]] = None
_allowlist_signature: Optional[str] = None
_allowlist_lock = threading.Lock()


def _get_allowlist() -> set[str]:
    """获取生效的白名单（内置默认 + 用户配置 redaction_key_allowlist）。配置变化时重建。"""
    global _allowlist_cache, _allowlist_signature
    raw = settings.redaction_key_allowlist or ""
    if _allowlist_cache is not None and _allowlist_signature == raw:
        return _allowlist_cache

    with _allowlist_lock:
        # double-check
        if _allowlist_cache is not None and _allowlist_signature == raw:
            return _allowlist_cache

        base = set(_DEFAULT_ALLOWLIST)
        for name in raw.split(","):
            name = name.strip().lower()
            if name:
                base.add(name)
        _allowlist_cache = base
        _allowlist_signature = raw
        return base


def is_sensitive_key(key) -> bool:
    """判断键名是否敏感：白名单优先（命中不脱敏），其次子串包含匹配。"""
    key_lower = str(key).lower()
    if key_lower in _get_allowlist():
        return False
    return any(s in key_lower for s in _SENSITIVE_SUBSTRINGS)


def redact_nested(value: Any) -> Any:
    """递归脱敏 dict / list 等嵌套结构：敏感键名整值掩码，字符串值走 redact()。"""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = redact_nested(item)
        return sanitized
    if isinstance(value, list):
        return [redact_nested(item) for item in value]
    if isinstance(value, tuple):
        return [redact_nested(item) for item in value]
    if isinstance(value, str):
        return redact(value) or value
    return value

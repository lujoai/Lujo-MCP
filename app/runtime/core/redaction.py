"""
敏感信息脱敏 —— 在数据进入存储 / 返回给 AI / 交给 LLM 前统一掩码。

设计要点（按 proj1 架构重新实现，非复制 proj2）：
- 纯函数 redact(text) -> str | None，对 None / 非字符串 / 空串原样返回。
- 默认覆盖常见密钥类字段：password / api_key / token / secret / Authorization / 手机号。
- 受 settings.redaction_enabled 控制，默认开启（fail-safe：宁可多掩也不泄露）。
- 额外正则由 settings.redaction_extra_patterns（换行分隔）提供，
  无效正则静默跳过不阻断主流程；编译结果按配置签名缓存，避免重复编译。
"""
import json
import re
import logging
import threading
from typing import Any, Optional

from app.config import settings
from app.utils.pattern_guard import DEFAULT_REDACT_RULES, compile_extra_rules

logger = logging.getLogger("lujo-mcp.redaction")

# 默认规则的字符串来源集中在 pattern_guard；仍在模块导入时编译，保持原时机。
_COMPILED_DEFAULT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern), replacement) for pattern, replacement in DEFAULT_REDACT_RULES
]

# 额外规则缓存（按配置内容签名，配置变化时重建）
_extra_cache: Optional[list[tuple["re.Pattern[str]", str]]] = None
_extra_signature: Optional[str] = None
_extra_lock = threading.Lock()

# U05-FIX / U05-QDRANT-FIX：灾难性回溯形态检测与逐行编译过滤收敛到中立
# 纯工具层 app/utils/pattern_guard.py（runtime 与 rag 都允许依赖），
# 与 qdrant embedding 外发路径共用同一份判定，防止副本语义再次漂移。


def _load_extra_rules() -> list[tuple["re.Pattern[str]", str]]:
    """编译并缓存用户配置的额外正则；配置变化时重新编译。线程安全。

    危险正则（灾难性回溯形态）不进入缓存，仅记 warning 安全摘要（不输出
    完整正则内容）；非法正则保持既有 warning + skip 行为不变。
    """
    global _extra_cache, _extra_signature
    # 快速路径：缓存命中
    if _extra_cache is not None and _extra_signature == (settings.redaction_extra_patterns or ""):
        return _extra_cache

    with _extra_lock:
        # double-check
        if _extra_cache is not None and _extra_signature == (settings.redaction_extra_patterns or ""):
            return _extra_cache

        raw = settings.redaction_extra_patterns or ""
        result = compile_extra_rules(raw)
        _extra_cache = list(result.rules)
        _extra_signature = raw

    # U06 收尾修复：warning 必须在锁释放后发出。app/utils/logging 的
    # JSONFormatter/RedactingFormatter 在 format() 里回调 redact()，若在本线程
    # 持 _extra_lock（非重入）期间发日志，会重入同一把锁造成自死锁
    # （py-spy 现场：全量 unit 卡死于 test_qdrant invalid-pattern）。
    for message in result.warnings:
        logger.warning(message)
    if result.dropped:
        reason_counts: dict[str, int] = {}
        for _, reason in result.dropped:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        reasons = ",".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
        logger.warning(
            "额外脱敏规则未生效 count=%d reasons=%s ——这些规则本应遮蔽的内容将原样进入存储与外发",
            len(result.dropped),
            reasons,
        )
    return _extra_cache


def redact(text: Optional[str]) -> Optional[str]:
    """对文本做脱敏。

    - None / 非字符串 / 空串：原样返回。
    - settings.redaction_enabled=False：原样返回。
    FIX(v0.7.1-b4-6): 脱敏关闭时仅首次告警——此前每次调用都 warning，
    高频上下文构建路径下日志刷屏。
    """
    if not isinstance(text, str) or not text:
        return text
    if not settings.redaction_enabled:
        _warn_redaction_disabled_once()
        return text
    for pattern, repl in _COMPILED_DEFAULT_RULES:
        text = pattern.sub(repl, text)
    for pattern, repl in _load_extra_rules():
        text = pattern.sub(repl, text)
    return text


# FIX(v0.7.1-b4-6): 脱敏关闭告警节流——模块级标志 + 锁只告警一次。
_redaction_disabled_warned = False
_redaction_disabled_lock = threading.Lock()


def _warn_redaction_disabled_once() -> None:
    global _redaction_disabled_warned
    if _redaction_disabled_warned:
        return
    with _redaction_disabled_lock:
        if _redaction_disabled_warned:
            return
        _redaction_disabled_warned = True
    # 与 _load_extra_rules 同理：redact() 在脱敏关闭时会回调本函数，
    # 持锁发日志将被 formatter 的 redact() 回调同线程重入死锁。
    logger.warning("redaction is disabled — sensitive data will NOT be masked")


# ── 结构化数据脱敏（dict/list 递归 + 键名白名单）────────────────────────────
# FIX: A2 —— 此前该逻辑内联在 trace_repo，logs.add_log 等直接写存储的路径
# 无法复用（trace_repo ↔ logs 存在循环 import），导致写入路径的原始
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
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                pass
            else:
                return json.dumps(
                    redact_nested(parsed), ensure_ascii=False, separators=(",", ":")
                )
        return redact(value) or value
    return value

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

# U05-FIX：灾难性回溯形态检测（保守策略）。
# 背景：`(a+)+b` 之类嵌套量词在 ≥28 字符输入上呈指数级回溯（实测 12.7s@28、
# >15s@30），经 redact() 生产路径可拖死调用线程。风险前提是**作者配置**了
# 危险正则（P2，非远程 P1）。
# 处置：沿用本模块既有「不可用规则即跳过、不阻断主流程」的降级语义，在配置期
# 拒绝装载危险正则。保守判定——只拦已确认的明确结构，宁可漏拦也不误伤常见
# 安全正则（如 `\b\d{17}[\dXx]\b`、`(?:foo|bar)-baz`、`prefix-\w+`）。
_DANGEROUS_REPEAT_RE = re.compile(
    r"\([^()]*[*+][^()]*\)[*+]"      # 组内量词 + 组外量词（嵌套量词）：(a+)+ / (a*)* / (\w+)+
    r"|\([^()|]*\|[^()|]*\)[*+]"     # 组内交替 + 组外量词（重叠重复）：(a|a)*
)


def _is_dangerous_pattern(pattern: str) -> bool:
    """检测正则是否含已确认的灾难性回溯形态（嵌套量词 / 重叠重复）。

    保守判定：只识别「括号组内已含量词或交替、且组外再叠加量词」的结构。
    安全形态（单层量词、非重叠交替、字符类内量词）不会被误判。
    """
    return _DANGEROUS_REPEAT_RE.search(pattern) is not None


def _load_extra_rules() -> list[tuple["re.Pattern[str]", str]]:
    """编译并缓存用户配置的额外正则；配置变化时重新编译。线程安全。

    U05-FIX：危险正则（灾难性回溯形态）不进入缓存，仅记 warning 安全摘要
    （不输出完整正则内容）；非法正则保持既有 warning + skip 行为不变。
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
        rules: list[tuple["re.Pattern[str]", str]] = []
        for index, line in enumerate(raw.splitlines(), 1):
            pattern = line.strip()
            if not pattern:
                continue
            if _is_dangerous_pattern(pattern):
                # 安全摘要：只报行号与长度，不输出正则内容——避免把作者可能
                # 用于匹配敏感数据的模式写进日志。
                logger.warning(
                    "跳过存在灾难性回溯风险的脱敏正则（第 %d 行，长度 %d）："
                    "嵌套量词/重叠重复结构在长输入上会指数级回溯，"
                    "请改写为单层量词或非重叠交替形式",
                    index,
                    len(pattern),
                )
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
    FIX(v0.7.1-b4-6): 脱敏关闭时仅首次告警——此前每次调用都 warning，
    高频上下文构建路径下日志刷屏。
    """
    if not isinstance(text, str) or not text:
        return text
    if not settings.redaction_enabled:
        _warn_redaction_disabled_once()
        return text
    for pattern, repl in _DEFAULT_RULES:
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
        logger.warning("redaction is disabled — sensitive data will NOT be masked")
        _redaction_disabled_warned = True


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

"""OpenAI 兼容 Chat Completions provider（M2-B2）。

定位：只做一次 HTTP 调用并把结果分类落账；不做评分、不做重试风暴、不 import
`app/`，也**不读取 .env**（配置只来自显式参数或 `os.environ` 中的
`BENCHMARK_LLM_*`，在调用时读取）。

安全：API Key 只出现在请求头构造的瞬间，绝不进入日志、manifest、异常文本或
落盘内容；所有对外文本一律先经 `redact()`。`redact()` / `redact_metadata()`
接受可选 `secrets`（真实已知凭据字面量），按字面量替换后再走通用正则——因为
正则只能覆盖有限形态，而 provider 已从 Authorization 头看到真实 key。
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from benchmark.hashing import text_hash

ENV_PREFIX = "BENCHMARK_LLM_"
ENV_BASE_URL = ENV_PREFIX + "BASE_URL"
ENV_API_KEY = ENV_PREFIX + "API_KEY"
ENV_MODEL = ENV_PREFIX + "MODEL"
ENV_TIMEOUT = ENV_PREFIX + "TIMEOUT"
ENV_TEMPERATURE = ENV_PREFIX + "TEMPERATURE"

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 1024

# 错误分类（稳定枚举，写入 record.execution.error_class）
ERROR_TIMEOUT = "timeout"
ERROR_CONNECTION = "connection_error"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_HTTP_4XX = "http_4xx"
ERROR_HTTP_5XX = "http_5xx"
ERROR_REDIRECT = "redirect_refused"
ERROR_INVALID_JSON = "invalid_json"
ERROR_EMPTY_COMPLETION = "empty_completion"
ERROR_NOT_CONFIGURED = "not_configured"

# 重定向是**拒绝**语义，绝不重试（重试只会再次把凭据送往未知目标）。
_RETRYABLE = frozenset({ERROR_TIMEOUT, ERROR_RATE_LIMITED, ERROR_HTTP_5XX})

_MAX_ERROR_TEXT = 400
_MAX_METADATA_TEXT = 200

_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?\S+", r"\1<redacted>"),
    (r"(?i)\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{6,}", "<redacted-key>"),
    (r"(?i)([?&](?:api[_-]?key|key|token|access_token)=)[^&\s\"']+", r"\1<redacted>"),
    (
        r"(?i)((?:api[_-]?key|apikey|secret|token|password|passwd)\s*[\"']?\s*[:=]\s*)"
        r"[\"']?[^\"'\s,}]+",
        r"\1<redacted>",
    ),
)


def normalize_secrets(secrets: Iterable[str] | None) -> tuple[str, ...]:
    """把已知凭据规整为「非空、去重、按长度降序」的元组。

    - 只忽略**空/纯空白** secret（空串会匹配任意位置，逐字符替换整段文本）。
    - **不按长度忽略**：任何非空 API Key 都必须脱敏——安全优先于文本保真，
      短 key 会造成较多文本替换，但绝不允许其明文落盘。
    - 按长度降序：单次替换的 alternation 是**最左优先**，必须先试最长的，
      否则短 secret 会先命中长 secret 的前缀，留下残片。
    """
    if not secrets:
        return ()
    uniq = {s for s in secrets if isinstance(s, str) and s.strip()}
    return tuple(sorted(uniq, key=len, reverse=True))


def _numeric_secret_values(secrets: tuple[str, ...]) -> set[Decimal]:
    """把已知凭据中**可无损解析为数字**的部分解析为 Decimal 集合。

    远端可把纯数字形态的 API Key 作为 JSON **number** 回显（`{"usage":{"n":123}}`），
    `json.loads` 得到 int/float，永远不会进入字符串字面量替换。这里预先把数字形态
    的 secret 解析为 `Decimal`，供数值标量做**精确、无精度损失**的等价比较。

    - 用 `Decimal`（而非 `float`）：`float("9876543210123456789")` 会丢精度，
      可能造成误匹配或漏匹配。
    - 非数字 secret 触发 `InvalidOperation`，静默跳过（它们只走字符串路径）。
    """
    values: set[Decimal] = set()
    for s in secrets:
        try:
            values.add(Decimal(s))
        except (InvalidOperation, ValueError, ArithmeticError):
            continue
    return values


def _matches_numeric_secret(value: bool | int | float, numeric: set[Decimal]) -> bool:
    """该数值标量是否等价于某个已知数字凭据（无精度损失）。

    bool 在 int 之前处理：`isinstance(True, int)` 为真，若先当数字会把 `True`
    与 `1` 混同。这里 bool 只在恰好等于数字凭据 `1`/`0` 时才视为匹配。
    """
    if not numeric:
        return False
    if isinstance(value, bool):
        # True/False 对应 1/0；仅当凭据恰为 1/0 时才算匹配。
        return Decimal(1 if value else 0) in numeric
    if isinstance(value, int):
        return Decimal(value) in numeric
    if isinstance(value, float):
        if not math.isfinite(value):
            return False  # inf/nan 不匹配任何有限凭据
        try:
            return Decimal(repr(value)) in numeric
        except (InvalidOperation, ValueError):
            return False
    return False


def _literal_pass(text: str, secrets: tuple[str, ...]) -> str:
    """单次、转义后的字面量替换（不重扫替换结果，故无二次污染/循环）。"""
    if not secrets or not text:
        return text
    pattern = "|".join(re.escape(s) for s in secrets)
    return re.sub(pattern, "<redacted-secret>", text)


def redact(text: Any, *, secrets: Iterable[str] | None = None) -> str:
    """脱敏任意文本。

    顺序：**先**已知凭据字面量替换（覆盖任意格式的真实 key），**再**跑通用
    正则 `_SECRET_PATTERNS` 作为补充防线。单次 `re.sub` 避免顺序替换互相污染。
    """
    out = text if isinstance(text, str) else str(text)
    out = _literal_pass(out, normalize_secrets(secrets))
    for pattern, replacement in _SECRET_PATTERNS:
        out = re.sub(pattern, replacement, out)
    return out


_SAFE_PLACEHOLDER = "<unavailable>"
_UNKNOWN_EXC_NAME = "Exception"


def _safe_exc_text(exc: Any, *, prefix: str = "") -> str:
    """安全提取异常的「类型名 + 文本」，供后续 `redact()` 脱敏。

    f-string 的 `f"{e}"` 会**先**调用 `str(e)` 再进入 `redact`——若 `__str__`/
    `__repr__` 自身抛携密异常，异常会在 `except` 块内逃逸，`redact` 无从执行。
    本 helper 保证：

    - 取类型名不依赖实例的 `__str__`/`__repr__`（`type(exc).__name__` 亦包在
      try 中，防元类 property 抛异常）。
    - `str()` 抛异常时**不再**对二次异常调用 str/repr，降级为固定占位符。
    - 自身绝不抛异常；输出仍交由 `redact()` 做字面量 + 正则脱敏。
    """
    try:
        name = type(exc).__name__
        if not isinstance(name, str) or not name:
            name = _UNKNOWN_EXC_NAME
    except BaseException:  # noqa: BLE001 - 元类 __name__ property 可抛
        name = _UNKNOWN_EXC_NAME
    try:
        detail = str(exc)
        if not isinstance(detail, str):
            detail = _SAFE_PLACEHOLDER
    except BaseException:  # noqa: BLE001 - 二次异常绝不 str/repr
        detail = _SAFE_PLACEHOLDER
    return f"{prefix}{name}: {detail}"


def _safe_redact_text(value: Any, secrets: tuple[str, ...] = ()) -> str:
    """把任意值转成「已脱敏」字符串；字符串化抛异常时返回固定安全占位符。

    绝不再调用可能再次泄漏数据的 `repr()`；绝不让原异常对象逃逸。
    """
    try:
        return redact(value, secrets=secrets)[:_MAX_METADATA_TEXT]
    except Exception:  # noqa: BLE001 - 字符串化/脱敏自身失败
        return _SAFE_PLACEHOLDER


def _safe_redact_key(
    key: Any, secrets: tuple[str, ...] = (), numeric: set[Decimal] | None = None
) -> str:
    """脱敏 dict key；标量 key 保持原值文本，其余走安全脱敏。

    数值 key 若等价于已知数字凭据，同样替换为占位符（JSON 对象的 key 恒为字符串，
    此分支主要覆盖直接 API 调用传入 Python `int`/`float` key 的情形）。
    """
    if key is None:
        return "None"
    if isinstance(key, (bool, int, float)):
        if numeric and _matches_numeric_secret(key, numeric):
            return "<redacted-secret>"
        return str(key)
    return _safe_redact_text(key, secrets)


def _redact_metadata(value: Any, known: tuple[str, ...], numeric: set[Decimal]) -> Any:
    """`redact_metadata` 的内部递归实现（`known`/`numeric` 已预计算，避免逐层重算）。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            key = _safe_redact_key(raw_key, known, numeric)
            if key in out:
                base, n = key, 2
                while key in out:
                    key = f"{base}-{n}"
                    n += 1
            out[key] = _redact_metadata(raw_val, known, numeric)
        return out
    if isinstance(value, list):
        return [_redact_metadata(v, known, numeric) for v in value]
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        # 数字形态凭据可被远端作为 JSON number 回显；仅当**精确匹配**已知凭据时
        # 才替换为占位符，否则保留原数值类型（不破坏 token 计数等 telemetry）。
        if _matches_numeric_secret(value, numeric):
            return "<redacted-secret>"
        return value
    return _safe_redact_text(value, known)


def redact_metadata(value: Any, *, secrets: Iterable[str] | None = None) -> Any:
    """递归脱敏 provider 返回的元数据（usage / finish_reason / request_id 等）。

    这些字段由**远端**提供，可能被构造来回显凭据；落盘前必须先脱敏并限长，
    绝不信任其内容。**key 与 value 都必须脱敏**——远端可以把密钥当成 JSON key
    回显（`{"usage": {"sk-...": 1}}`）。

    - dict / list 递归；tuple 等其它类型退化为已脱敏字符串。
    - 非敏感 key 经 `redact` 后保持原值文本。
    - 多个敏感 key 脱敏后同名时**追加数字后缀**（`<redacted-key>`、
      `<redacted-key-2>`），绝不静默覆盖丢失。
    - key/value 字符串化抛异常时返回固定占位符，绝不向外抛携密异常。
    - `secrets` 为已知真实凭据（如 `api_key`），按字面量脱敏后再走正则。
    - **数字标量**：精确匹配已知数字凭据时替换为占位符，否则保留原值。
    """
    known = normalize_secrets(secrets)
    numeric = _numeric_secret_values(known)
    return _redact_metadata(value, known, numeric)


@dataclass(slots=True)
class LLMConfig:
    """一次实验运行的 provider 配置（显式传入，不从 .env 读取）。"""

    base_url: str
    api_key: str
    model: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_retries: int = 0

    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def endpoint_host(self) -> str:
        """仅 scheme+host[:port]+path，**剥离** userinfo 与 query。

        用于复现记录：绝不能把 `https://user:pw@host` 的凭据或 `?api_key=` 写进
        manifest / 日志。
        """
        from urllib.parse import urlsplit

        parts = urlsplit(self.base_url)
        host = parts.hostname or ""
        netloc = host
        if parts.port is not None:
            netloc = f"{host}:{parts.port}"
        return f"{parts.scheme}://{netloc}{parts.path}".rstrip("/")


class LLMNotConfiguredError(RuntimeError):
    """provider 未配置（缺 base_url / api_key / model）。"""


def load_config_from_env(
    env: dict[str, str] | None = None,
    *,
    override: dict[str, Any] | None = None,
) -> LLMConfig:
    """从环境变量 + 显式 override 组装配置；缺失时抛 `LLMNotConfiguredError`。

    只读 `BENCHMARK_LLM_*`（不回落 `OPENAI_API_KEY`，避免误用生产凭据）。
    每次调用时读取，便于测试 monkeypatch。
    """
    src = os.environ if env is None else env
    ov = override or {}

    base_url = ov.get("base_url") or src.get(ENV_BASE_URL) or ""
    api_key = ov.get("api_key") or src.get(ENV_API_KEY) or ""
    model = ov.get("model") or src.get(ENV_MODEL) or ""
    missing = [
        name
        for name, value in (
            (ENV_BASE_URL, base_url),
            (ENV_API_KEY, api_key),
            (ENV_MODEL, model),
        )
        if not value
    ]
    if missing:
        raise LLMNotConfiguredError(
            "benchmark LLM provider is not configured; missing "
            + ", ".join(missing)
            + " (set the environment variables or pass the matching CLI flags)"
        )

    timeout_raw = ov.get("timeout_s", src.get(ENV_TIMEOUT, DEFAULT_TIMEOUT_S))
    temperature_raw = ov.get("temperature", src.get(ENV_TEMPERATURE, DEFAULT_TEMPERATURE))
    try:
        timeout_s = float(timeout_raw)
    except (TypeError, ValueError) as e:
        raise LLMNotConfiguredError(f"{ENV_TIMEOUT} must be a number") from e
    try:
        temperature = float(temperature_raw)
    except (TypeError, ValueError) as e:
        raise LLMNotConfiguredError(f"{ENV_TEMPERATURE} must be a number") from e
    # 非有限值（nan / inf / -inf / 1e999）会写出无法通过 validate 的 Manifest，
    # 必须在进入 provider 之前拒绝。
    if not math.isfinite(temperature):
        raise LLMNotConfiguredError(f"{ENV_TEMPERATURE} must be a finite number")
    if not math.isfinite(timeout_s):
        raise LLMNotConfiguredError(f"{ENV_TIMEOUT} must be a finite number")

    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_s=timeout_s,
        temperature=temperature,
        max_tokens=int(ov.get("max_tokens", DEFAULT_MAX_TOKENS)),
        max_retries=int(ov.get("max_retries", 0)),
    )


@dataclass(slots=True)
class LLMResult:
    """一次调用的结果（成功或失败都返回，不抛异常给调用方）。

    `text` 只在成功时有值；失败时 `error_class` 非空、`error_message` 已脱敏。
    """

    ok: bool
    text: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    attempts: int = 1
    retry_after_s: float | None = None
    response_sha256: str | None = None
    body_sha256: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    request_id: str | None = None


# transport 契约：接受 (url, headers, body_bytes, timeout_s) → (status, body_text, headers)
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, str, dict[str, str]]]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝一切 HTTP 重定向。

    标准库默认的 `HTTPRedirectHandler` 会把请求（**包括 Authorization 头**）
    重发到 `Location` 指向的目标，而该目标可能是任意 origin——等于把 API Key
    交给攻击者控制的服务器。返回 `None` 会让 urllib 停止跟随并抛出携带原始
    状态码的 `HTTPError`，调用方据此落账为明确的失败。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# 独立 opener，不装配重定向 handler；无可变全局状态。
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout_s: float
) -> tuple[int, str, dict[str, str]]:
    """默认 transport：标准库 HTTP POST（无第三方依赖），**拒绝重定向**。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout_s) as resp:  # noqa: S310
            return int(resp.status), resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp is not None else ""
        return int(e.code), raw, dict(e.headers or {})


def _extract_text(payload: Any) -> tuple[str | None, dict[str, Any], str | None]:
    """从 chat completions 响应提取文本、usage、finish_reason。"""
    if not isinstance(payload, dict):
        return None, {}, None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, {}, None
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    finish = first.get("finish_reason")
    return (content if isinstance(content, str) else None), usage, (finish if isinstance(finish, str) else None)


def classify_http_error(status: int) -> str:
    if status == 429:
        return ERROR_RATE_LIMITED
    if 300 <= status < 400:
        # 未跟随的重定向：独立分类，绝不与 4xx 混淆，也不可重试。
        return ERROR_REDIRECT
    if 400 <= status < 500:
        return ERROR_HTTP_4XX
    if status >= 500:
        return ERROR_HTTP_5XX
    return ERROR_HTTP_4XX


class LLMProvider:
    """最小 OpenAI 兼容 provider；`transport` 可注入（测试用 fake，生产用默认）。"""

    def __init__(self, config: LLMConfig, transport: Transport | None = None) -> None:
        self.config = config
        self._transport: Transport = transport or _urllib_transport

    def call(self, messages: list[dict[str, str]]) -> LLMResult:
        """调用一次（含显式重试上限）；任何失败都以 LLMResult 表达，不抛异常。"""
        last: LLMResult | None = None
        attempts = 0
        max_attempts = max(1, self.config.max_retries + 1)
        while attempts < max_attempts:
            attempts += 1
            result = self._call_once(messages, attempts)
            result.attempts = attempts
            if result.ok:
                return result
            last = result
            if result.error_class not in _RETRYABLE or attempts >= max_attempts:
                return result
            delay = result.retry_after_s
            if delay is None:
                delay = min(2.0 ** (attempts - 1), 30.0)
            time.sleep(max(0.0, min(float(delay), 30.0)))
        return last if last is not None else LLMResult(ok=False, error_class=ERROR_CONNECTION)

    def _call_once(self, messages: list[dict[str, str]], attempt: int) -> LLMResult:
        secrets = normalize_secrets((self.config.api_key,))
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        started = time.monotonic()
        try:
            status, text, resp_headers = self._transport(
                self.config.chat_completions_url(),
                headers,
                body,
                self.config.timeout_s,
            )
        except (TimeoutError, socket.timeout):
            return LLMResult(
                ok=False,
                error_class=ERROR_TIMEOUT,
                error_message=f"request timed out after {self.config.timeout_s}s",
                latency_ms=self._elapsed_ms(started),
            )
        except urllib.error.URLError as e:
            return LLMResult(
                ok=False,
                error_class=ERROR_CONNECTION,
                error_message=redact(
                    _safe_exc_text(e.reason, prefix="connection error: "), secrets=secrets
                )[:_MAX_ERROR_TEXT],
                latency_ms=self._elapsed_ms(started),
            )
        except OSError as e:
            return LLMResult(
                ok=False,
                error_class=ERROR_CONNECTION,
                error_message=redact(
                    _safe_exc_text(e, prefix="network error: "), secrets=secrets
                )[:_MAX_ERROR_TEXT],
                latency_ms=self._elapsed_ms(started),
            )
        except Exception as e:  # noqa: BLE001 - 兜底：任何异常都必须脱敏后落账，
            # 绝不让原始异常（可能含 Authorization / API Key 回显）逃逸到 traceback。
            return LLMResult(
                ok=False,
                error_class=ERROR_CONNECTION,
                error_message=redact(_safe_exc_text(e), secrets=secrets)[:_MAX_ERROR_TEXT],
                latency_ms=self._elapsed_ms(started),
            )

        latency_ms = self._elapsed_ms(started)
        # 远端可控元数据先脱敏再进入 LLMResult，杜绝凭据回显落盘。
        request_id = (
            redact_metadata(resp_headers.get("x-request-id"), secrets=secrets)
            if resp_headers
            else None
        )
        retry_after = self._retry_after(resp_headers)

        if status != 200:
            return LLMResult(
                ok=False,
                error_class=classify_http_error(status),
                error_message=redact(text, secrets=secrets)[:_MAX_ERROR_TEXT],
                http_status=status,
                latency_ms=latency_ms,
                retry_after_s=retry_after,
                body_sha256=text_hash(text),
                request_id=request_id,
            )

        try:
            payload = json.loads(text)
        except ValueError:
            return LLMResult(
                ok=False,
                error_class=ERROR_INVALID_JSON,
                error_message="response body is not valid JSON",
                http_status=status,
                latency_ms=latency_ms,
                body_sha256=text_hash(text),
                request_id=request_id,
            )

        content, usage, finish_reason = _extract_text(payload)
        # 模型输出同样是远端可控文本，可能被诱导回显凭据（并会写入 raw-dir）。
        # 先留一份原文用于 response_sha256（单向摘要，不泄漏明文），再脱敏正文。
        raw_content = content
        content = redact(content, secrets=secrets) if content is not None else None
        usage = redact_metadata(usage, secrets=secrets)
        finish_reason = redact_metadata(finish_reason, secrets=secrets)
        if not content or not content.strip():
            return LLMResult(
                ok=False,
                error_class=ERROR_EMPTY_COMPLETION,
                error_message="model returned no textual content",
                http_status=status,
                latency_ms=latency_ms,
                body_sha256=text_hash(text),
                usage=usage,
                finish_reason=finish_reason,
                request_id=request_id,
            )

        return LLMResult(
            ok=True,
            text=content,
            http_status=status,
            latency_ms=latency_ms,
            response_sha256=text_hash(raw_content) if raw_content is not None else None,
            body_sha256=text_hash(text),
            usage=usage,
            finish_reason=finish_reason,
            request_id=request_id,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _retry_after(headers: dict[str, str] | None) -> float | None:
        if not headers:
            return None
        for key, value in headers.items():
            if key.lower() == "retry-after":
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None


__all__ = [
    "ENV_BASE_URL",
    "ENV_API_KEY",
    "ENV_MODEL",
    "ENV_TIMEOUT",
    "ENV_TEMPERATURE",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "ERROR_TIMEOUT",
    "ERROR_CONNECTION",
    "ERROR_RATE_LIMITED",
    "ERROR_HTTP_4XX",
    "ERROR_HTTP_5XX",
    "ERROR_REDIRECT",
    "ERROR_INVALID_JSON",
    "ERROR_EMPTY_COMPLETION",
    "ERROR_NOT_CONFIGURED",
    "LLMConfig",
    "LLMNotConfiguredError",
    "LLMProvider",
    "LLMResult",
    "load_config_from_env",
    "classify_http_error",
    "redact",
    "redact_metadata",
    "normalize_secrets",
]

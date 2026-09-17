"""OpenAI 兼容 Chat Completions provider（M2-B2）。

定位：只做一次 HTTP 调用并把结果分类落账；不做评分、不做重试风暴、不 import
`app/`，也**不读取 .env**（配置只来自显式参数或 `os.environ` 中的
`BENCHMARK_LLM_*`，在调用时读取）。

安全：API Key 只出现在请求头构造的瞬间，绝不进入日志、manifest、异常文本或
落盘内容；所有对外文本一律先经 `redact()`。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

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


def redact(text: Any) -> str:
    """脱敏任意文本：去除 Authorization / API Key / token 等敏感片段。"""
    import re

    out = text if isinstance(text, str) else str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        out = re.sub(pattern, replacement, out)
    return out


def redact_metadata(value: Any) -> Any:
    """递归脱敏 provider 返回的元数据（usage / finish_reason / request_id 等）。

    这些字段由**远端**提供，可能被构造来回显凭据；落盘前必须先脱敏并限长，
    绝不信任其内容。仅处理 dict / list / str / 标量，保留 JSON 可序列化性。
    """
    if isinstance(value, dict):
        return {str(k): redact_metadata(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_metadata(v) for v in value]
    if isinstance(value, str):
        return redact(value)[:_MAX_METADATA_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(value)[:_MAX_METADATA_TEXT]


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
                error_message=redact(f"connection error: {e.reason!r}"),
                latency_ms=self._elapsed_ms(started),
            )
        except OSError as e:
            return LLMResult(
                ok=False,
                error_class=ERROR_CONNECTION,
                error_message=redact(f"network error: {e}"),
                latency_ms=self._elapsed_ms(started),
            )
        except Exception as e:  # noqa: BLE001 - 兜底：任何异常都必须脱敏后落账，
            # 绝不让原始异常（可能含 Authorization / API Key 回显）逃逸到 traceback。
            return LLMResult(
                ok=False,
                error_class=ERROR_CONNECTION,
                error_message=redact(f"{type(e).__name__}: {e}"),
                latency_ms=self._elapsed_ms(started),
            )

        latency_ms = self._elapsed_ms(started)
        # 远端可控元数据先脱敏再进入 LLMResult，杜绝凭据回显落盘。
        request_id = (
            redact_metadata(resp_headers.get("x-request-id")) if resp_headers else None
        )
        retry_after = self._retry_after(resp_headers)

        if status != 200:
            return LLMResult(
                ok=False,
                error_class=classify_http_error(status),
                error_message=redact(text)[:_MAX_ERROR_TEXT],
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
        usage = redact_metadata(usage)
        finish_reason = redact_metadata(finish_reason)
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
            response_sha256=text_hash(content),
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
]

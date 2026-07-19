"""LLM 错误分析器（加强版）—— 重试、超时、上下文截断、流式输出"""

import copy
import json
import logging
import time
from typing import Optional, Generator

from openai import OpenAI, APIError, APITimeoutError, RateLimitError

from app.config import settings
from app.mcp.core.redaction import redact

logger = logging.getLogger("ai-debug-mcp.llm")

_client: Optional[OpenAI] = None
SENSITIVE_KEYS = {
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "passwd",
    "pwd",
}


_PROVIDER_BASE_URLS = {
    "openai": "",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/",
    "custom": "",
}


def _resolve_base_url() -> str:
    """确定 base_url：显式配置优先 → provider 默认 → 空（OpenAI 默认）"""
    return settings.llm_base_url or _PROVIDER_BASE_URLS.get(settings.llm_provider, "")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = settings.openai_api_key
        if not api_key:
            raise RuntimeError("请在 .env 中配置有效的 OPENAI_API_KEY")

        base_url = _resolve_base_url()
        kwargs = {
            "api_key": api_key,
            "timeout": settings.llm_timeout,
            "max_retries": 0,  # 我们自己控制重试
        }
        if base_url:
            kwargs["base_url"] = base_url

        _client = OpenAI(**kwargs)
    return _client


SYSTEM_PROMPT = """你是一位资深的后端排障专家。用户会提供程序运行时的上下文信息（请求流、异常堆栈、系统状态等），请分析并输出 JSON：

{
  "root_cause": "问题根因 —— 问题出在哪一步、为什么会发生",
  "impact": "影响面 —— 是否会导致数据不一致/服务中断/安全风险等",
  "fix": "修复建议 —— 具体的代码修改方案",
  "confidence": "置信度 high/medium/low"
}

只输出 JSON，不要包含其他文字。"""


def _redact_value_for_llm(value):
    """递归脱敏发送给外部 LLM 的上下文。"""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = _redact_value_for_llm(item)
        return sanitized
    if isinstance(value, list):
        return [_redact_value_for_llm(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value_for_llm(item) for item in value]
    if isinstance(value, str):
        return redact(value) or value
    return value


def _prepare_context_for_llm(context: dict) -> dict:
    """发送给外部模型前，先截断再递归脱敏。"""
    truncated = truncate_context(copy.deepcopy(context))
    return _redact_value_for_llm(truncated)


def build_analysis_prompt(context: dict) -> str:
    """将调试上下文构建为 LLM 提示文本（用于调试和展示）"""
    context = _prepare_context_for_llm(context)
    parts = []
    parts.append(f"请求 ID: {context.get('request_id', 'N/A')}")
    flow = context.get("flow", [])
    if flow:
        parts.append(f"执行流程: {' → '.join(flow)}")
    input_data = context.get("input")
    if input_data:
        parts.append(f"输入数据: {json.dumps(input_data, ensure_ascii=False, indent=2)}")
    output_data = context.get("output")
    if output_data:
        parts.append(f"输出数据: {json.dumps(output_data, ensure_ascii=False, indent=2)}")
    errors = context.get("errors", [])
    if errors:
        parts.append(f"错误信息: {json.dumps(errors, ensure_ascii=False, indent=2)}")
    exception = context.get("exception")
    if exception:
        parts.append(f"异常详情: {json.dumps(exception, ensure_ascii=False, indent=2)}")
    runtime = context.get("runtime")
    if runtime:
        parts.append(f"运行时状态: {json.dumps(runtime, ensure_ascii=False, indent=2)}")
    return "\n\n".join(parts)


def truncate_context(context: dict, max_tokens: Optional[int] = None) -> dict:
    """截断上下文，防止超过 token 限制"""
    max_tokens = max_tokens or settings.max_context_tokens
    # 简单估算：1 token ≈ 2 中文字 ≈ 4 英文字符
    max_chars = max_tokens * 3

    # 截断运行时快照
    runtime = context.get("runtime")
    if runtime:
        # 只保留关键字段
        runtime = {
            "python": runtime.get("python", {}),
            "system": {
                "cpu_percent": runtime.get("system", {}).get("cpu_percent"),
                "memory_percent": runtime.get("system", {}).get("memory_percent"),
            },
            "process": {
                "pid": runtime.get("process", {}).get("pid"),
                "cpu_percent": runtime.get("process", {}).get("cpu_percent"),
                "memory_rss_mb": runtime.get("process", {}).get("memory_rss_mb"),
                "num_threads": runtime.get("process", {}).get("num_threads"),
            },
        }

    # 截断异常帧
    exc = context.get("exception")
    if exc and "frames" in exc:
        max_frames = settings.max_stack_frames
        max_locals = settings.max_locals_per_frame
        frames = exc["frames"]
        if len(frames) > max_frames:
            exc["frames"] = frames[:max_frames] + [
                {"_note": f"... 省略了 {len(frames) - max_frames} 帧"}
            ]
        for f in exc["frames"]:
            if "locals" in f and len(f["locals"]) > max_locals:
                local_keys = list(f["locals"].keys())[:max_locals]
                f["locals"] = {k: f["locals"][k] for k in local_keys}

    # 最终截断：序列化后按字符数裁剪
    serialized = json.dumps(context, ensure_ascii=False, default=str)
    if len(serialized) > max_chars:
        context = {
            "request_id": context.get("request_id"),
            "flow": context.get("flow"),
            "errors": context.get("errors"),
            "exception": context.get("exception"),
            "_truncated": True,
            "_note": f"上下文过长已截断（{len(serialized)} → {max_chars} 字符）",
        }
        # 不保留完整的 input/output/runtime
        if context.get("input"):
            context["input"] = str(context["input"])[:500]
        if context.get("output"):
            context["output"] = str(context["output"])[:500]

    return context


def _retry_call(
    client: OpenAI,
    model: str,
    messages: list,
    temperature: float,
    max_retries: int,
) -> dict:
    """带重试的 LLM 调用"""
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            choice = response.choices[0]
            content = choice.message.content or "{}"

            # 尝试解析 LLM 返回的 JSON
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = {"analysis": content}

            return {
                "analysis": parsed,
                "model": response.model,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                    "total_tokens": response.usage.total_tokens if response.usage else 0,
                },
                "attempts": attempt + 1,
            }

        except RateLimitError as e:
            last_error = e
            wait = min(2 ** attempt, 30)
            logger.warning(f"LLM rate limit, retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries:
                time.sleep(wait)

        except APITimeoutError as e:
            last_error = e
            logger.warning(f"LLM timeout, retrying (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries:
                time.sleep(1)

        except APIError as e:
            last_error = e
            logger.error(f"LLM API error on attempt {attempt + 1}: {e}")
            if attempt < max_retries:
                time.sleep(1)

    # 所有重试都失败，尝试 fallback 模型
    if model != settings.llm_fallback_model and settings.llm_fallback_model:
        logger.warning(f"主模型 {model} 不可用，切换到 fallback: {settings.llm_fallback_model}")
        return _retry_call(
            client, settings.llm_fallback_model,
            messages[:3],  # fallback 时缩短 prompt
            temperature, 1,  # 只重试 1 次
        )

    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_error}")


def analyze(context: dict, model: Optional[str] = None) -> dict:
    """
    调用 LLM 分析调试上下文（带重试和 fallback）
    """
    client = _get_client()
    model_name = model or settings.llm_model

    context = _prepare_context_for_llm(context)

    prompt_str = json.dumps(context, ensure_ascii=False, default=str)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"请分析以下调试上下文：\n\n{prompt_str}"},
    ]

    start = time.time()
    result = _retry_call(
        client, model_name, messages,
        temperature=settings.llm_temperature,
        max_retries=settings.llm_max_retries,
    )
    elapsed = time.time() - start

    logger.info(
        "LLM analysis complete",
        extra={
            "model": result["model"],
            "elapsed_s": round(elapsed, 2),
            "attempts": result["attempts"],
            "tokens": result["usage"],
        },
    )

    return result


def analyze_stream(context: dict, model: Optional[str] = None) -> Generator[str, None, None]:
    """
    流式 LLM 分析（用于 SSE）
    """
    client = _get_client()
    model_name = model or settings.llm_model

    context = _prepare_context_for_llm(context)
    prompt_str = json.dumps(context, ensure_ascii=False, default=str)

    stream = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请分析以下调试上下文：\n\n{prompt_str}"},
        ],
        temperature=settings.llm_temperature,
        stream=True,
    )

    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

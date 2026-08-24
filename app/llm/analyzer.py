"""LLM 错误分析仪（编排层）—— 重试、fallback、熔断器、流式输出。

god object 重构（v0.5.5+）：原 1175 行拆分为 7 个单一职责模块，本文件
只保留**调用编排**：

- ``app.llm.clients``          客户端工厂 + provider 分派
- ``app.llm.cache``            L1 LRU + L2 Redis 多级缓存 + 指纹
- ``app.llm.injection_guard``  Prompt Injection 防护（公开 API）
- ``app.llm.context_prep``     脱敏 / 截断 / 提示文本构建 / 错误信号提取
- ``app.llm.output_schema``    LLM 输出 JSON 提取 + Schema 校验净化
- ``app.llm.kb_integration``   KB 三级命中 + 向量 RAG 召回 + 经验回写

同步/异步重试已收敛（原先 _retry_call 双轨 130 行逐行重复）：
异常分类 / 成功格式化 / fallback 判定抽为共享内核，sync/async 仅剩
IO 调用与 sleep 语义差异。
"""

import asyncio
import copy
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Generator, AsyncGenerator

from openai import OpenAI, AsyncOpenAI, APIError, APITimeoutError, RateLimitError

from app.config import settings
from app.llm.clients import _get_client, _get_async_client
from app.llm.cache import _get_cached_result, _set_cache_result, _compute_context_fingerprint
from app.llm.injection_guard import INJECTION_GUARD, wrap_evidence
from app.llm.context_prep import (
    _prepare_context_for_llm,
    _get_error_fingerprint,
)
from app.llm.output_schema import _validate_and_normalize
from app.llm.kb_integration import (
    _get_knowledge_base_result,
    _persist_analysis_to_knowledge_base,
)

logger = logging.getLogger("lujo-mcp.llm")

# ── 熔断器（P3-8）──
try:
    import pybreaker
    _CB_ERROR = pybreaker.CircuitBreakerError
    _CB_STATE_OPEN = pybreaker.STATE_OPEN
except ImportError:
    pybreaker = None
    _CB_ERROR = None
    _CB_STATE_OPEN = None
    logger.warning("pybreaker 未安装，熔断器功能已禁用")


_llm_circuit_breaker = None
_llm_circuit_breaker_lock = threading.Lock()


def _get_llm_circuit_breaker():
    global _llm_circuit_breaker
    if _llm_circuit_breaker is not None:
        return _llm_circuit_breaker
    if not pybreaker or not settings.circuit_breaker_enabled:
        return None
    with _llm_circuit_breaker_lock:
        if _llm_circuit_breaker is None:
            _llm_circuit_breaker = pybreaker.CircuitBreaker(
                fail_max=settings.cb_llm_max_failures,
                reset_timeout=settings.cb_llm_reset_timeout,
                exclude=[pybreaker.CircuitBreakerError],
            )
    return _llm_circuit_breaker


def _llm_fallback_result() -> dict:
    return {
        "analysis": {
            "root_cause": "LLM 服务暂时不可用（熔断器已触发）",
            "impact": "分析功能降级，返回默认分析结果",
            "fix": "请稍后重试，或联系管理员检查 LLM 服务状态",
            "confidence": "low",
            "reasoning_chain": [],
            "evidence_items": [],
        },
        "model": "__circuit_breaker_fallback__",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "attempts": 0,
        "cached": False,
        "knowledge_base_hit": False,
        "analysis_source": "fallback",
        "_circuit_breaker_triggered": True,
    }


SYSTEM_PROMPT = """你是一位资深的后端排障专家。用户会提供程序运行时的上下文信息（请求流、异常堆栈、系统状态等），请分析并输出 JSON：

{
  "root_cause": "问题根因 —— 问题出在哪一步、为什么会发生",
  "impact": "影响面 —— 是否会导致数据不一致/服务中断/安全风险等",
  "fix": "修复建议 —— 具体的代码修改方案",
  "confidence": "置信度 high/medium/low",
  "reasoning_chain": [
    "推理步骤 1：从堆栈帧中发现异常类型为 X，发生在文件 Y 的第 Z 行",
    "推理步骤 2：结合上下文判断，该异常的触发条件为...",
    "推理步骤 3：综合以上分析，得出根因结论..."
  ],
  "evidence_items": [
    {
      "type": "证据类型（stack_trace/code_snippet/git_blame/runtime_state/network_capture）",
      "description": "从上下文中提取到的关键证据描述",
      "relevance": "high/medium/low"
    }
  ]
}

只输出 JSON，不要包含其他文字。""" + INJECTION_GUARD


def _annotate_analysis_result(
    result: dict,
    *,
    knowledge_base_hit: bool,
    analysis_source: str,
) -> dict:
    annotated = copy.deepcopy(result)
    annotated["knowledge_base_hit"] = knowledge_base_hit
    annotated["analysis_source"] = analysis_source
    return annotated


# ── 重试共享内核（sync/async 双轨收敛）──

def _classify_llm_error(exc: Exception, attempt: int, max_retries: int) -> Optional[float]:
    """LLM 调用异常的重试决策：返回等待秒数，None 表示该异常不重试。

    日志语义与原双轨实现一致：rate limit → warning（含退避秒数），
    timeout → warning，API error → error 级别。
    """
    if isinstance(exc, RateLimitError):
        wait = min(2 ** attempt, 30)
        logger.warning(f"LLM rate limit, retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
        return wait
    if isinstance(exc, APITimeoutError):
        logger.warning(f"LLM timeout, retrying (attempt {attempt + 1}/{max_retries})")
        return 1.0
    if isinstance(exc, APIError):
        logger.error(f"LLM API error on attempt {attempt + 1}: {exc}")
        return 1.0
    return None


def _format_llm_success(response, attempt: int) -> dict:
    """把 OpenAI chat 响应格式化为统一分析结果结构（含输出校验净化）。"""
    choice = response.choices[0]
    content = choice.message.content or "{}"

    return {
        "analysis": _validate_and_normalize(content),
        "model": response.model,
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
            "completion_tokens": response.usage.completion_tokens if response.usage else 0,
            "total_tokens": response.usage.total_tokens if response.usage else 0,
        },
        "attempts": attempt + 1,
    }


def _fallback_target(model: str) -> Optional[str]:
    """主模型失败后可切换的 fallback 模型名；无则返回 None。"""
    fb = settings.llm_fallback_model
    if model != fb and fb:
        return fb
    return None


def _retry_call(
    client: OpenAI,
    model: str,
    messages: list,
    temperature: float,
    max_retries: int,
) -> dict:
    """带重试的 LLM 调用（同步版，重试内核与异步版共享）"""
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            return _format_llm_success(response, attempt)

        except (RateLimitError, APITimeoutError, APIError) as e:
            last_error = e
            wait = _classify_llm_error(e, attempt, max_retries)
            if wait is not None and attempt < max_retries:
                time.sleep(wait)

    # 所有重试都失败，尝试 fallback 模型
    fb = _fallback_target(model)
    if fb:
        logger.warning(f"主模型 {model} 不可用，切换到 fallback: {fb}")
        return _retry_call(
            client, fb,
            messages[:3],  # fallback 时缩短 prompt
            temperature, 1,  # 只重试 1 次
        )

    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_error}")


async def _retry_call_async(
    client: AsyncOpenAI,
    model: str,
    messages: list,
    temperature: float,
    max_retries: int,
) -> dict:
    """带重试的异步 LLM 调用（await asyncio.sleep，重试内核与同步版共享）"""
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            return _format_llm_success(response, attempt)

        except (RateLimitError, APITimeoutError, APIError) as e:
            last_error = e
            wait = _classify_llm_error(e, attempt, max_retries)
            if wait is not None and attempt < max_retries:
                await asyncio.sleep(wait)

    # 所有重试都失败，尝试 fallback 模型
    fb = _fallback_target(model)
    if fb:
        logger.warning(f"主模型 {model} 不可用，切换到 fallback: {fb}")
        return await _retry_call_async(
            client, fb,
            messages[:3],  # fallback 时缩短 prompt
            temperature, 1,  # 只重试 1 次
        )

    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_error}")


# ── 编排共享流程 ──

def _build_llm_messages(context: dict) -> tuple[list, dict]:
    """脱敏 + 截断后构建 chat messages。返回 (messages, prepared_context)。"""
    context = _prepare_context_for_llm(context)
    prompt_str = json.dumps(context, ensure_ascii=False, default=str)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": wrap_evidence(prompt_str)},
    ]
    return messages, context


def _finalize_analysis(
    result: dict,
    fingerprint: str,
    kb_fingerprint: Optional[str],
    context: dict,
    elapsed: float,
) -> dict:
    """LLM 成功后的收尾：写缓存 + KB 回写 + 日志 + 来源注解（同步）。"""
    _set_cache_result(fingerprint, copy.deepcopy(result))
    _persist_analysis_to_knowledge_base(kb_fingerprint, result, context)

    logger.info(
        "LLM analysis complete",
        extra={
            "model": result["model"],
            "elapsed_s": round(elapsed, 2),
            "attempts": result["attempts"],
            "tokens": result["usage"],
            "cached": False,
        },
    )

    result["cached"] = False
    return _annotate_analysis_result(
        result,
        knowledge_base_hit=False,
        analysis_source="llm",
    )


def _call_through_circuit_breaker(sync_call):
    """通过熔断器执行同步调用；熔断触发时返回 fallback 结果，其余异常原样抛出。"""
    start = time.time()
    try:
        cb = _get_llm_circuit_breaker()
        if cb:
            return cb.call(sync_call), start
        return sync_call(), start
    except Exception as exc:
        # pybreaker 未安装时为 None；此时无熔断器异常，须原样抛出，避免
        # 求值 `pybreaker.CircuitBreakerError` 时因 None 触发 AttributeError 掩盖真实异常。
        if pybreaker and isinstance(exc, pybreaker.CircuitBreakerError):
            logger.warning("LLM 熔断器已触发，返回 fallback 结果")
            return _llm_fallback_result(), start
        raise


async def _call_async_through_breaker(cb, coro_factory):
    """在原生 asyncio 上下文中驱动 pybreaker 熔断状态机。

    pybreaker 的 ``CircuitBreaker.call`` 与 ``call_async``（tornado 协程）均不支持
    原生 asyncio；此处复刻其 closed/open/half-open 语义，并复用当前 state 对象的
    ``_handle_success``/``_handle_error`` 完成失败计数与状态迁移，使熔断开启时
    也能走 AsyncOpenAI（不再退回 to_thread 同步客户端），判定语义与同步 ``analyze`` 一致。
    OPEN 且未到重置时间 → 抛 ``pybreaker.CircuitBreakerError``（由调用方转 fallback）。

    FIX: v0.6.6 事件循环阻塞 —— pybreaker 内部为 threading.RLock，与同步路径
    （to_thread 中的 ``analyze``/``cb.call``）争锁时事件循环线程会持锁等待。
    三段锁临界区（状态检查/失败计数/成功计数）整体移入线程池执行，
    锁争用不再发生在事件循环线程；状态对象仍在同一临界区内捕获/使用，语义不变。
    """

    def _state_check():
        with cb._lock:
            state = cb.state
            if cb.current_state == _CB_STATE_OPEN:
                opened_at = cb._state_storage.opened_at
                if opened_at and datetime.now(timezone.utc) < opened_at + timedelta(
                    seconds=cb.reset_timeout
                ):
                    raise _CB_ERROR(
                        "Timeout not elapsed yet, circuit breaker still open"
                    )
                cb.half_open()
                state = cb.state
            return state

    state = await asyncio.to_thread(_state_check)
    coro = coro_factory()  # 仅构造协程，不执行
    try:
        result = await coro
    except BaseException as exc:
        # 按熔断语义重新抛出：未达阈值→原异常；达阈值/半开失败→CircuitBreakerError
        # （except ... as 变量在块尾会被删除，闭包内引用需先显式绑定）
        err = exc

        def _record_error():
            with cb._lock:
                state._handle_error(err)

        await asyncio.to_thread(_record_error)
        raise
    else:
        def _record_success():
            with cb._lock:
                state._handle_success()

        await asyncio.to_thread(_record_success)
        return result


def analyze(context: dict, model: Optional[str] = None) -> dict:
    """
    调用 LLM 分析调试上下文（带重试、fallback、缓存和熔断器）

    P1-2: 按上下文 fingerprint 缓存分析结果，避免相同问题重复调用 LLM。
    P3-8: 熔断器保护，当 LLM 服务连续失败时触发熔断，返回结构化 fallback。
    """
    knowledge_base_result = _get_knowledge_base_result(context)
    if knowledge_base_result:
        return knowledge_base_result

    fingerprint = _compute_context_fingerprint(context)
    knowledge_base_fingerprint = _get_error_fingerprint(context)
    cached = _get_cached_result(fingerprint)
    if cached:
        logger.info("LLM analysis cache hit (fingerprint=%s)", fingerprint)
        cached["cached"] = True
        return _annotate_analysis_result(
            cached,
            knowledge_base_hit=False,
            analysis_source="llm",
        )

    client = _get_client()
    model_name = model or settings.llm_model

    messages, context = _build_llm_messages(context)

    def _call_llm():
        return _retry_call(
            client, model_name, messages,
            temperature=settings.llm_temperature,
            max_retries=settings.llm_max_retries,
        )

    result, start = _call_through_circuit_breaker(_call_llm)
    if result.get("_circuit_breaker_triggered"):
        return result
    elapsed = time.time() - start

    return _finalize_analysis(result, fingerprint, knowledge_base_fingerprint, context, elapsed)


async def analyze_async(context: dict, model: Optional[str] = None) -> dict:
    """
    异步调用 LLM 分析调试上下文（带重试、fallback、多级缓存和熔断器）

    Phase 3.2：用 AsyncOpenAI 替代同步客户端，await 调用 + asyncio.sleep 重试。
    缓存逻辑复用 _get_cached_result/_set_cache_result（已支持 L1+L2 多级缓存），
    用 asyncio.to_thread 包装避免阻塞事件循环。
    P3-8: 熔断器保护，当 LLM 服务连续失败时触发熔断，返回结构化 fallback。
    """
    knowledge_base_result = await asyncio.to_thread(_get_knowledge_base_result, context)
    if knowledge_base_result:
        return knowledge_base_result

    fingerprint = _compute_context_fingerprint(context)
    knowledge_base_fingerprint = _get_error_fingerprint(context)
    cached = await asyncio.to_thread(_get_cached_result, fingerprint)
    if cached:
        logger.info("LLM analysis cache hit (fingerprint=%s)", fingerprint)
        cached["cached"] = True
        return _annotate_analysis_result(
            cached,
            knowledge_base_hit=False,
            analysis_source="llm",
        )

    client = _get_async_client()
    model_name = model or settings.llm_model

    messages, context = _build_llm_messages(context)

    # v0.6.1：pybreaker 的 CircuitBreaker.call 与 tornado call_async 均不支持原生
    # asyncio，改用 _call_async_through_breaker 手动驱动状态机（复用其 _handle_success /
    # _handle_error 计数），使熔断开启时也走 AsyncOpenAI，不再退回 to_thread 同步客户端。
    start = time.time()
    try:
        cb = _get_llm_circuit_breaker()
        if cb:
            result = await _call_async_through_breaker(
                cb,
                lambda: _retry_call_async(
                    client, model_name, messages,
                    temperature=settings.llm_temperature,
                    max_retries=settings.llm_max_retries,
                ),
            )
        else:
            result = await _retry_call_async(
                client, model_name, messages,
                temperature=settings.llm_temperature,
                max_retries=settings.llm_max_retries,
            )
    except Exception as exc:
        if pybreaker and isinstance(exc, pybreaker.CircuitBreakerError):
            logger.warning("LLM 熔断器已触发，返回 fallback 结果")
            return _llm_fallback_result()
        raise
    elapsed = time.time() - start

    return await asyncio.to_thread(
        _finalize_analysis, result, fingerprint, knowledge_base_fingerprint, context, elapsed
    )


def analyze_stream(context: dict, model: Optional[str] = None) -> Generator[str, None, None]:
    """
    流式 LLM 分析（用于 SSE）
    """
    client = _get_client()
    model_name = model or settings.llm_model

    messages, _ = _build_llm_messages(context)

    stream = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=settings.llm_temperature,
        stream=True,
    )

    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def analyze_stream_async(context: dict, model: Optional[str] = None) -> AsyncGenerator[str, None]:
    """
    异步流式 LLM 分析（用于 SSE）

    Phase 3.2：用 AsyncOpenAI 替代同步生成器，原生 async for 迭代。
    """
    client = _get_async_client()
    model_name = model or settings.llm_model

    messages, _ = _build_llm_messages(context)

    stream = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=settings.llm_temperature,
        stream=True,
    )

    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

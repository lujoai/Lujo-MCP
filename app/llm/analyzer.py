"""LLM 错误分析器（加强版）—— 重试、超时、上下文截断、流式输出、输出校验净化、熔断器"""

import copy
import json
import logging
import re
import time
import asyncio
import hashlib
import threading
from collections import OrderedDict
from typing import Optional, Generator, AsyncGenerator

from openai import OpenAI, AsyncOpenAI, APIError, APITimeoutError, RateLimitError

from app.config import settings
from app.mcp.core.redaction import redact

logger = logging.getLogger("ai-debug-mcp.llm")

# ── 熔断器（P3-8）──
try:
    import pybreaker
except ImportError:
    pybreaker = None
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
        },
        "model": "__circuit_breaker_fallback__",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "attempts": 0,
        "cached": False,
        "_circuit_breaker_triggered": True,
    }

_client: Optional[OpenAI] = None
# _client_lock：模块级 bool 标志，作为轻量自旋锁使用。
# 线程安全性：Python GIL 保证对 bool 的读写是原子的；多线程并发时
# 仅一个线程能将 False → True 成功，其余线程进入自旋等待。
# 模块级创建在 import 时即存在，不存在延迟初始化的竞态问题。
_client_lock: bool = False

# ── 异步 OpenAI 客户端（Phase 3.2）──
_async_client: Optional[AsyncOpenAI] = None
# _async_client_lock：threading.Lock 保护 _async_client 的双重检查锁。
# 线程安全性：threading.Lock 在模块级创建是安全的——Python GIL 确保
# Lock 对象本身的创建是原子的，import 完成后锁已就绪，后续多线程
# 共享同一把锁实例，双重检查模式保证只初始化一次 AsyncOpenAI。
_async_client_lock = threading.Lock()

# ── L2 Redis 缓存客户端（Phase 3.3）──
# 惰性初始化；Redis 不可用时为 None，降级为仅 L1 内存缓存
_redis_cache_client: Optional[object] = None
_redis_cache_initialized: bool = False
_redis_cache_lock = threading.Lock()
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

# ── LLM 分析结果缓存（P1-2）──
# 按 fingerprint 缓存 LLM 分析结果，避免相同上下文重复调用
# 使用 OrderedDict 实现真正的 LRU：
#   - 命中时 move_to_end(key)，把最近访问的条目移到链表末尾
#   - 容量超限时 popitem(last=False)，淘汰链表头部（最久未访问）的条目
_MAX_CACHE_SIZE = 100
_CACHE_TTL_SECONDS = 3600
_analysis_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()


def _compute_context_fingerprint(context: dict) -> str:
    """计算上下文指纹，用于缓存命中判定"""
    key_parts = [
        context.get("exception", {}).get("fingerprint", ""),
        context.get("request_id", ""),
        str(context.get("errors", "")),
    ]
    return hashlib.sha256("|".join(key_parts).encode()).hexdigest()[:16]


def _get_cached_result(fingerprint: str) -> Optional[dict]:
    """获取缓存结果（多级缓存 L1+L2），未命中或过期则返回 None。

    查找顺序：L1(OrderedDict LRU) → L2(Redis)。
    L2 命中时回填 L1。Redis 不可用时静默降级为仅 L1。
    返回深拷贝以保护缓存不可变性。
    """
    # ── L1: OrderedDict LRU ──
    with _cache_lock:
        entry = _analysis_cache.get(fingerprint)
        if entry:
            if time.time() - entry["cached_at"] > _CACHE_TTL_SECONDS:
                del _analysis_cache[fingerprint]
                entry = None
            else:
                # LRU：命中后移到末尾，保持"末尾=最近访问"顺序
                _analysis_cache.move_to_end(fingerprint)
                return copy.deepcopy(entry["result"])

    # ── L2: Redis ──
    redis_client = _get_redis_cache()
    if redis_client is not None:
        try:
            raw = redis_client.get(f"ai-debug:llm:cache:{fingerprint}")
            if raw:
                result = json.loads(raw)
                # L2 命中 → 回填 L1
                _set_cache_result(fingerprint, result)
                logger.info("LLM cache L2 hit (fingerprint=%s)", fingerprint)
                return copy.deepcopy(result)
        except Exception:
            logger.warning("L2 Redis 缓存读取失败，降级为 L1", exc_info=True)

    return None


def _set_cache_result(fingerprint: str, result: dict) -> None:
    """设置缓存结果（多级缓存 L1+L2）。

    写 L1(OrderedDict LRU，超容量淘汰最久未使用) + L2(Redis, TTL=3600s)。
    Redis 不可用时静默降级为仅 L1。
    """
    # ── L1: OrderedDict LRU ──
    with _cache_lock:
        is_new = fingerprint not in _analysis_cache
        if is_new and len(_analysis_cache) >= _MAX_CACHE_SIZE:
            # 容量已满且为新键：淘汰链表头部最久未访问的条目
            _analysis_cache.popitem(last=False)
        _analysis_cache[fingerprint] = {
            "result": result,
            "cached_at": time.time(),
        }
        if not is_new:
            # 已存在键：赋值不改变位置，显式移到末尾标记最近使用
            _analysis_cache.move_to_end(fingerprint)

    # ── L2: Redis ──
    redis_client = _get_redis_cache()
    if redis_client is not None:
        try:
            redis_client.setex(
                f"ai-debug:llm:cache:{fingerprint}",
                _CACHE_TTL_SECONDS,
                json.dumps(result, ensure_ascii=False, default=str),
            )
        except Exception:
            logger.warning("L2 Redis 缓存写入失败，降级为 L1", exc_info=True)


_PROVIDER_BASE_URLS = {
    "openai": "",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/",
    "custom": "",
}


def _resolve_base_url() -> str:
    """确定 base_url：显式配置优先 → provider 默认 → 空（OpenAI 默认）"""
    return settings.llm_base_url or _PROVIDER_BASE_URLS.get(settings.llm_provider, "")


def _get_client() -> OpenAI:
    global _client, _client_lock
    if _client is None:
        if _client_lock:
            # 其他线程正在创建，等待后返回
            for _ in range(50):
                time.sleep(0.01)
                if _client is not None:
                    return _client
            raise RuntimeError("OpenAI client 初始化超时")
        _client_lock = True
        try:
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
        finally:
            _client_lock = False
    return _client


def _get_redis_cache():
    """惰性获取 Redis 客户端（L2 缓存），不可用时返回 None。

    Redis 不可用时静默降级为仅 L1 内存缓存，不影响功能。
    采用双重检查 + threading.Lock 保证线程安全。
    """
    global _redis_cache_client, _redis_cache_initialized
    if _redis_cache_initialized:
        return _redis_cache_client
    with _redis_cache_lock:
        if not _redis_cache_initialized:
            try:
                import redis as _redis_module
                client = _redis_module.Redis.from_url(
                    settings.redis_url,
                    socket_timeout=2,
                    decode_responses=True,
                )
                client.ping()  # 测试连接可用性
                _redis_cache_client = client
                logger.info("LLM L2 Redis 缓存已连接")
            except Exception:
                logger.warning("Redis L2 缓存不可用，降级为仅 L1 内存缓存")
                _redis_cache_client = None
            finally:
                _redis_cache_initialized = True
    return _redis_cache_client


def _get_async_client() -> AsyncOpenAI:
    """获取 AsyncOpenAI 客户端（惰性初始化，线程安全）。

    复用 _resolve_base_url 的 provider 分派逻辑。
    """
    global _async_client
    if _async_client is None:
        with _async_client_lock:
            if _async_client is None:  # 双重检查
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

                _async_client = AsyncOpenAI(**kwargs)
    return _async_client


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


VALID_CONFIDENCE = {"high", "medium", "low"}
REQUIRED_FIELDS = ("root_cause", "impact", "fix")
MAX_FIELD_CHARS = 2000
MAX_RAW_TRUNCATED = 500


def _extract_json(content: str) -> Optional[str]:
    """从 LLM 输出中提取 JSON 字符串，支持 markdown code block。"""
    stripped = content.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
        if match:
            return match.group(1).strip()
    # 尝试找最外层 {} 或 []（非贪婪匹配，取第一个）
    match = re.search(r'(\{.*?\}|\[.*?\])', stripped, re.DOTALL)
    if match:
        return match.group(1)
    return None


def _truncate_field(value: str, max_chars: int) -> str:
    """截断字符串到指定长度。"""
    if not isinstance(value, str):
        value = str(value)
    if len(value) > max_chars:
        return value[:max_chars]
    return value


def _validate_and_normalize(raw_output: str) -> dict:
    """
    校验并净化 LLM 输出，确保符合 {root_cause, impact, fix, confidence} 契约。

    步骤：
      1. 容错 JSON 提取（支持 markdown code block、嵌套文本）
      2. Schema 校验（字段齐全 + confidence 合法）
      3. 字段长度截断
      4. 仍失败返回结构化 fallback
    """
    # Step 1: 尝试解析 JSON
    parsed = None
    parse_succeeded = False
    try:
        parsed = json.loads(raw_output)
        parse_succeeded = True
    except (json.JSONDecodeError, TypeError):
        extracted = _extract_json(raw_output)
        if extracted:
            try:
                parsed = json.loads(extracted)
                parse_succeeded = True
            except (json.JSONDecodeError, TypeError):
                pass

    # null / 数组 / 基本类型 → 视为无法解析为对象
    if not isinstance(parsed, dict):
        parsed = {}
        parse_succeeded = False

    # Step 2: 字段校验与默认值
    result = {}
    for field in REQUIRED_FIELDS:
        val = parsed.get(field, "")
        result[field] = _truncate_field(val, MAX_FIELD_CHARS) if val else ""

    # confidence: 缺失或无效 → "low"
    confidence = parsed.get("confidence")
    if not confidence or confidence not in VALID_CONFIDENCE:
        confidence = "low"
    result["confidence"] = confidence

    # Step 3: 解析失败时添加 raw_truncated
    if not parse_succeeded:
        result["raw_truncated"] = _truncate_field(raw_output, MAX_RAW_TRUNCATED)

    return result


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

            # 校验并净化 LLM 输出
            analysis = _validate_and_normalize(content)

            return {
                "analysis": analysis,
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


async def _retry_call_async(
    client: AsyncOpenAI,
    model: str,
    messages: list,
    temperature: float,
    max_retries: int,
) -> dict:
    """带重试的异步 LLM 调用（用 await asyncio.sleep 替代 time.sleep）"""
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            choice = response.choices[0]
            content = choice.message.content or "{}"

            # 校验并净化 LLM 输出
            analysis = _validate_and_normalize(content)

            return {
                "analysis": analysis,
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
                await asyncio.sleep(wait)

        except APITimeoutError as e:
            last_error = e
            logger.warning(f"LLM timeout, retrying (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries:
                await asyncio.sleep(1)

        except APIError as e:
            last_error = e
            logger.error(f"LLM API error on attempt {attempt + 1}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(1)

    # 所有重试都失败，尝试 fallback 模型
    if model != settings.llm_fallback_model and settings.llm_fallback_model:
        logger.warning(f"主模型 {model} 不可用，切换到 fallback: {settings.llm_fallback_model}")
        return await _retry_call_async(
            client, settings.llm_fallback_model,
            messages[:3],  # fallback 时缩短 prompt
            temperature, 1,  # 只重试 1 次
        )

    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_error}")


def analyze(context: dict, model: Optional[str] = None) -> dict:
    """
    调用 LLM 分析调试上下文（带重试、fallback、缓存和熔断器）

    P1-2: 按上下文 fingerprint 缓存分析结果，避免相同问题重复调用 LLM。
    P3-8: 熔断器保护，当 LLM 服务连续失败时触发熔断，返回结构化 fallback。
    """
    fingerprint = _compute_context_fingerprint(context)
    cached = _get_cached_result(fingerprint)
    if cached:
        logger.info("LLM analysis cache hit (fingerprint=%s)", fingerprint)
        cached["cached"] = True
        return cached

    client = _get_client()
    model_name = model or settings.llm_model

    context = _prepare_context_for_llm(context)

    prompt_str = json.dumps(context, ensure_ascii=False, default=str)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"请分析以下调试上下文：\n\n{prompt_str}"},
    ]

    def _call_llm():
        return _retry_call(
            client, model_name, messages,
            temperature=settings.llm_temperature,
            max_retries=settings.llm_max_retries,
        )

    start = time.time()
    try:
        cb = _get_llm_circuit_breaker()
        if cb:
            result = cb.call(_call_llm)
        else:
            result = _call_llm()
    except pybreaker.CircuitBreakerError:
        logger.warning("LLM 熔断器已触发，返回 fallback 结果")
        return _llm_fallback_result()
    elapsed = time.time() - start

    _set_cache_result(fingerprint, copy.deepcopy(result))

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
    return result


async def analyze_async(context: dict, model: Optional[str] = None) -> dict:
    """
    异步调用 LLM 分析调试上下文（带重试、fallback、多级缓存和熔断器）

    Phase 3.2：用 AsyncOpenAI 替代同步客户端，await 调用 + asyncio.sleep 重试。
    缓存逻辑复用 _get_cached_result/_set_cache_result（已支持 L1+L2 多级缓存），
    用 asyncio.to_thread 包装避免阻塞事件循环。
    P3-8: 熔断器保护，当 LLM 服务连续失败时触发熔断，返回结构化 fallback。
    """
    fingerprint = _compute_context_fingerprint(context)
    cached = await asyncio.to_thread(_get_cached_result, fingerprint)
    if cached:
        logger.info("LLM analysis cache hit (fingerprint=%s)", fingerprint)
        cached["cached"] = True
        return cached

    client = _get_async_client()
    model_name = model or settings.llm_model

    context = _prepare_context_for_llm(context)

    prompt_str = json.dumps(context, ensure_ascii=False, default=str)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"请分析以下调试上下文：\n\n{prompt_str}"},
    ]

    async def _call_llm():
        return await _retry_call_async(
            client, model_name, messages,
            temperature=settings.llm_temperature,
            max_retries=settings.llm_max_retries,
        )

    start = time.time()
    try:
        cb = _get_llm_circuit_breaker()
        if cb:
            result = await asyncio.to_thread(
                cb.call,
                lambda: asyncio.run(_call_llm()),
            )
        else:
            result = await _call_llm()
    except pybreaker.CircuitBreakerError:
        logger.warning("LLM 熔断器已触发，返回 fallback 结果")
        return _llm_fallback_result()
    elapsed = time.time() - start

    await asyncio.to_thread(_set_cache_result, fingerprint, copy.deepcopy(result))

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


async def analyze_stream_async(context: dict, model: Optional[str] = None) -> AsyncGenerator[str, None]:
    """
    异步流式 LLM 分析（用于 SSE）

    Phase 3.2：用 AsyncOpenAI 替代同步生成器，原生 async for 迭代。
    """
    client = _get_async_client()
    model_name = model or settings.llm_model

    context = _prepare_context_for_llm(context)
    prompt_str = json.dumps(context, ensure_ascii=False, default=str)

    stream = await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请分析以下调试上下文：\n\n{prompt_str}"},
        ],
        temperature=settings.llm_temperature,
        stream=True,
    )

    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

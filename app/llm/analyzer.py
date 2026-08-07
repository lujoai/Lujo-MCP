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
from app.rag.knowledge_base import (
    get_knowledge_entry,
    get_entry_by_normalized_fingerprint,
    get_entries_by_type_fingerprint,
    retrieve_similar,
    upsert_knowledge_entry,
)
from app.rag.debug_case import (
    compute_normalized_fingerprint,
    compute_type_fingerprint,
    normalize_message_for_similarity,
)
from app.rag.vector_store import get_vector_store
from app.runtime.core.redaction import redact

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

_client: Optional[OpenAI] = None
_client_lock = threading.Lock()

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


def _set_l1_only(fingerprint: str, result: dict) -> None:
    """仅写入 L1 缓存（OrderedDict LRU），不写 L2、不刷新 L2 TTL。

    仅供 ``app.llm.cache_prewarm`` 使用——预热场景下 L2 已有数据，
    若调 ``_set_cache_result`` 会 ``setex`` 刷新 L2 TTL，导致定时预热
    周期下 L2 永不自然淘汰，违反 TTL 淘汰语义。本函数让 L2 TTL 自然流逝，
    该过期的过期，下次 SCAN 时自然不在结果集里。

    LRU 逻辑与 ``_set_cache_result`` 的 L1 段完全一致：容量满且新键时
    ``popitem(last=False)`` 淘汰最久未访问；已存在键 ``move_to_end``。
    必须与 ``_set_cache_result`` 共享 ``_cache_lock`` 避免并发竞态。
    """
    with _cache_lock:
        is_new = fingerprint not in _analysis_cache
        if is_new and len(_analysis_cache) >= _MAX_CACHE_SIZE:
            _analysis_cache.popitem(last=False)
        _analysis_cache[fingerprint] = {
            "result": result,
            "cached_at": time.time(),
        }
        if not is_new:
            _analysis_cache.move_to_end(fingerprint)


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
        with _client_lock:
            if _client is None:
                api_key = settings.openai_api_key
                if not api_key:
                    raise RuntimeError("请在 .env 中配置有效的 OPENAI_API_KEY")

                base_url = _resolve_base_url()
                kwargs = {
                    "api_key": api_key,
                    "timeout": settings.llm_timeout,
                    "max_retries": 0,
                }
                if base_url:
                    kwargs["base_url"] = base_url

                _client = OpenAI(**kwargs)
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
    校验并净化 LLM 输出，确保符合 {root_cause, impact, fix, confidence, reasoning_chain, evidence_items} 契约。

    步骤：
      1. 容错 JSON 提取（支持 markdown code block、嵌套文本）
      2. Schema 校验（字段齐全 + confidence 合法）
      3. 字段长度截断
      4. v0.4.0 新增 reasoning_chain / evidence_items（缺失时默认空列表，向后兼容）
      5. 仍失败返回结构化 fallback
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

    # v0.4.0: reasoning_chain —— 推理步骤链（缺失时默认空列表，向后兼容旧输出）
    reasoning_chain = parsed.get("reasoning_chain")
    if isinstance(reasoning_chain, list):
        result["reasoning_chain"] = [
            _truncate_field(str(s), MAX_FIELD_CHARS) for s in reasoning_chain
        ]
    else:
        result["reasoning_chain"] = []

    # v0.4.0: evidence_items —— LLM 提取的证据条目（缺失时默认空列表，向后兼容）
    evidence_items = parsed.get("evidence_items")
    if isinstance(evidence_items, list):
        valid_items = []
        for item in evidence_items:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                desc = _truncate_field(str(item.get("description", "")), MAX_FIELD_CHARS)
                relevance = item.get("relevance", "medium")
                if relevance not in ("high", "medium", "low"):
                    relevance = "medium"
                valid_items.append({
                    "type": _truncate_field(str(item_type), 100),
                    "description": desc,
                    "relevance": relevance,
                })
        result["evidence_items"] = valid_items[:10]  # 最多 10 条
    else:
        result["evidence_items"] = []

    # Step 3: 解析失败时添加 raw_truncated
    if not parse_succeeded:
        result["raw_truncated"] = _truncate_field(raw_output, MAX_RAW_TRUNCATED)

    return result


def _get_error_signal(context: dict) -> tuple[str, str, Optional[str]]:
    """从调试上下文提取 (异常类型, 异常消息, 精确指纹)。

    优先从 context.exception 取，其次遍历 context.errors。
    返回的 type/message 可为空串（无法提取时），fingerprint 可为 None。
    """
    exception = context.get("exception")
    if isinstance(exception, dict):
        return (
            str(exception.get("type") or exception.get("exception_type") or ""),
            str(exception.get("message") or exception.get("msg") or ""),
            str(exception["fingerprint"]) if exception.get("fingerprint") else None,
        )

    for error in context.get("errors", []) or []:
        if isinstance(error, dict):
            return (
                str(error.get("type") or error.get("exception_type") or ""),
                str(error.get("message") or error.get("msg") or ""),
                str(error["fingerprint"]) if error.get("fingerprint") else None,
            )

    return "", "", None


def _get_error_fingerprint(context: dict) -> Optional[str]:
    _, _, fingerprint = _get_error_signal(context)
    return fingerprint


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


def _get_knowledge_base_result(context: dict) -> Optional[dict]:
    exc_type, message, fingerprint = _get_error_signal(context)
    if not fingerprint:
        return None

    # L1：精确指纹命中
    entry = get_knowledge_entry(fingerprint)
    if entry is not None:
        return _build_kb_result(entry, "knowledge_base")

    # L1.5：归一化指纹命中（同模式、不同变量值）
    if exc_type or message:
        normalized_fp = compute_normalized_fingerprint(exc_type, message)
        entry = get_entry_by_normalized_fingerprint(normalized_fp)
        if entry is not None:
            return _build_kb_result(entry, "knowledge_base_normalized")

    # L2：类型级 Jaccard 兜底（同类型异常，消息 token 重叠）
    if settings.kb_type_level_fallback and exc_type:
        type_fp = compute_type_fingerprint(exc_type)
        candidates = get_entries_by_type_fingerprint(type_fp, top_k=5)
        entry = _best_type_fallback(candidates, message)
        if entry is not None:
            return _build_kb_result(entry, "knowledge_base_type")

    # 精确指纹 miss → 向量检索 RAG fallback（二级召回）
    return _try_vector_rag(context, fingerprint)


def _best_type_fallback(
    candidates: list[dict], message: str
) -> dict | None:
    """在 L2 候选里按消息 Jaccard 相似度选最优（低于阈值返回 None）。"""
    if not candidates or not message:
        return None
    query_tokens = set(normalize_message_for_similarity(message).split())
    if not query_tokens:
        return None
    min_score = settings.kb_seed_jaccard_min_score
    best_entry: dict | None = None
    best_score = 0.0
    for cand in candidates:
        cand_msg = str(
            (cand.get("analysis") or {}).get("message")
            or (cand.get("_kb_meta") or {}).get("message")
            or ""
        )
        cand_tokens = set(normalize_message_for_similarity(cand_msg).split())
        if not cand_tokens:
            continue
        inter = len(query_tokens & cand_tokens)
        union = len(query_tokens | cand_tokens)
        score = inter / union if union else 0.0
        if score >= min_score and score > best_score:
            best_score = score
            best_entry = cand
    return best_entry


def _build_kb_result(entry: dict, analysis_source: str) -> dict:
    """把 KB entry 封装为 LLM 分析结果结构（与现有 knowledge_base 分支一致）。"""
    analysis = copy.deepcopy(entry.get("analysis") or {})
    fix_suggestion = entry.get("fix_suggestion")
    if fix_suggestion and not analysis.get("fix"):
        analysis["fix"] = fix_suggestion
    return {
        "analysis": analysis,
        "model": "__knowledge_base__",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "attempts": 0,
        "cached": False,
        "knowledge_base_hit": True,
        "analysis_source": analysis_source,
    }


def _try_vector_rag(context: dict, fingerprint: str) -> Optional[dict]:
    """向量检索 RAG fallback：精确指纹 miss 后按相似度召回历史分析。

    返回 None 表示无相似结果，调用方应继续走 LLM 链路。
    """
    try:
        query_text = json.dumps(context, ensure_ascii=False, default=str)
        similar = retrieve_similar(query_text)
    except Exception:
        logger.warning("Vector retrieval failed", exc_info=True)
        return None
    if not similar:
        return None

    doc = similar[0]
    analysis = copy.deepcopy(doc.get("analysis") or {})
    fix_suggestion = doc.get("fix_suggestion") or analysis.get("fix")
    if fix_suggestion and not analysis.get("fix"):
        analysis["fix"] = fix_suggestion

    logger.info("Vector RAG hit (fingerprint=%s)", fingerprint)
    return {
        "analysis": analysis,
        "model": "__vector_rag__",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "attempts": 0,
        "cached": False,
        "knowledge_base_hit": False,
        "analysis_source": "vector_rag",
    }


def _persist_analysis_to_knowledge_base(
    fingerprint: Optional[str], result: dict, context: Optional[dict] = None
) -> None:
    if not fingerprint:
        return

    analysis = result.get("analysis")
    if not isinstance(analysis, dict):
        return

    # 注入异常类型/消息，支撑三级 fallback（L1.5 归一化 / L2 类型级）
    if context:
        exc_type, message, _ = _get_error_signal(context)
        persist_analysis = copy.deepcopy(analysis)
        persist_analysis.setdefault("exception_type", exc_type)
        persist_analysis.setdefault("message", message)
    else:
        persist_analysis = analysis

    try:
        upsert_knowledge_entry(
            fingerprint=fingerprint,
            analysis=persist_analysis,
            fix_suggestion=analysis.get("fix", ""),
            source="llm",
        )
    except Exception:
        logger.warning(
            "Knowledge base auto-persist failed (fingerprint=%s)",
            fingerprint,
            exc_info=True,
        )

    # 向量检索 RAG：将分析结果同步写入向量库，供未来相似问题召回
    try:
        get_vector_store().add([{
            "fingerprint": fingerprint,
            "analysis": persist_analysis,
            "fix_suggestion": analysis.get("fix", ""),
            "source": "llm",
        }])
    except Exception:
        logger.warning(
            "Vector store persist failed (fingerprint=%s)",
            fingerprint,
            exc_info=True,
        )


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
    except Exception as exc:
        # pybreaker 未安装时为 None；此时无熔断器异常，须原样抛出，避免
        # 求值 `pybreaker.CircuitBreakerError` 时因 None 触发 AttributeError 掩盖真实异常。
        if pybreaker and isinstance(exc, pybreaker.CircuitBreakerError):
            logger.warning("LLM 熔断器已触发，返回 fallback 结果")
            return _llm_fallback_result()
        raise
    elapsed = time.time() - start

    _set_cache_result(fingerprint, copy.deepcopy(result))
    _persist_analysis_to_knowledge_base(knowledge_base_fingerprint, result, context)

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
            sync_client = _get_client()
            result = await asyncio.to_thread(
                cb.call,
                lambda: _retry_call(
                    sync_client, model_name, messages,
                    temperature=settings.llm_temperature,
                    max_retries=settings.llm_max_retries,
                ),
            )
        else:
            result = await _call_llm()
    except Exception as exc:
        # pybreaker 未安装时为 None；此时无熔断器异常，须原样抛出，避免
        # 求值 `pybreaker.CircuitBreakerError` 时因 None 触发 AttributeError 掩盖真实异常。
        if pybreaker and isinstance(exc, pybreaker.CircuitBreakerError):
            logger.warning("LLM 熔断器已触发，返回 fallback 结果")
            return _llm_fallback_result()
        raise
    elapsed = time.time() - start

    await asyncio.to_thread(_set_cache_result, fingerprint, copy.deepcopy(result))
    await asyncio.to_thread(_persist_analysis_to_knowledge_base, knowledge_base_fingerprint, result, context)

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

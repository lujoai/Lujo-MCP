"""调试上下文预处理 —— 错误信号提取、脱敏、截断、提示文本构建。

从 analyzer.py 拆出（god object 重构）：发送给外部 LLM 前的上下文加工链。
``_get_error_signal`` 是纯提取函数，被 cache.py（指纹计算）与
kb_integration.py（KB 命中）共同依赖，故置于本模块（依赖最底层）。
"""

import copy
import json

from app.config import settings
from app.runtime.core.redaction import redact, is_sensitive_key


def _get_error_signal(context: dict) -> tuple[str, str, "str | None"]:
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


def _get_error_fingerprint(context: dict) -> "str | None":
    _, _, fingerprint = _get_error_signal(context)
    return fingerprint


def _redact_value_for_llm(value):
    """递归脱敏发送给外部 LLM 的上下文。

    键名判定复用 redaction.is_sensitive_key（子串包含 + 白名单，FIX: A2
    自 trace_repo 下沉），使 user_token / db_password / apikey / x-api-key
    等复合键同样被脱敏，与入库脱敏策略保持一致。"""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if is_sensitive_key(key):
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


def truncate_context(context: dict, max_tokens: "int | None" = None) -> dict:
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
        # 精简结果必须写回，否则后续序列化/最终截断仍基于未精简的完整 runtime
        context["runtime"] = runtime

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
        # 暂存旧值，截断后恢复（新 dict 不含 input/output）
        old_input = context.get("input")
        old_output = context.get("output")
        context = {
            "request_id": context.get("request_id"),
            "flow": context.get("flow"),
            "errors": context.get("errors"),
            "exception": context.get("exception"),
            "_truncated": True,
            "_note": f"上下文过长已截断（{len(serialized)} → {max_chars} 字符）",
        }
        if old_input:
            context["input"] = str(old_input)[:500]
        if old_output:
            context["output"] = str(old_output)[:500]

    # 二次校验：errors/exception 自身超大时，截断后仍可能超 max_chars
    serialized2 = json.dumps(context, ensure_ascii=False, default=str)
    if len(serialized2) > max_chars:
        budget = max(1000, max_chars // 3)
        for field in ("errors", "exception"):
            if field in context:
                field_str = json.dumps(context[field], ensure_ascii=False, default=str)
                if len(field_str) > budget:
                    context[field] = field_str[:budget] + " ...(二次截断)"
        context["_note"] = f"上下文仍过长，已二次硬截断（{len(serialized2)} 字符）"

    return context

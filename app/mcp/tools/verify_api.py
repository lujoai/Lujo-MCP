"""
MCP 工具：verify —— 比对实际结果 vs 期望规范，自动检测静默失败。

输入 actual（实际结果）+ spec（期望规范）或 spec_id（已存储规范的 ID）。
输出 VerifyResult = {matched, diffs, silent_failure, spec_id?, trace_id?}

spec 与 spec_id 二选一：传 spec 直接比对；传 spec_id 从 spec_store 取已存储规范。

M1-B: 验证成功（matched=true）且关联了 trace_id 时，可选写回 KB ——
递增 verify_count、提升 case_confidence。验证失败不写回。
"""
import logging

from app.runtime.verifier.assert_engine import assert_behavior
from app.runtime.verifier import spec_store

logger = logging.getLogger("lujo-mcp.tools.verify")

VERIFY_DEF = {
    "name": "verify",
    "description": (
        "比对实际结果与期望规范，自动检测静默失败（返回 200/无报错但行为不符规范）。"
        "适合用户说「接口没报错但功能不对」「返回了但数据是空的」这类问题。"
        "传入 actual（实际结果）+ [spec | spec_id | sample] 三选一："
        "若提供 sample（成功基线样本），将自动推导规范基线并与 actual 比对，无需手写 spec。"
        "返回 {matched, diffs, silent_failure}；matched=false 且无异常、无 4xx/5xx 时 silent_failure=true。"
        "运行时排错请先用 diagnose_issue 拿现场。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "actual": {
                "type": "object",
                "description": "实际结果，含 status_code、body、error 等字段",
            },
            "spec": {
                "type": "object",
                "description": "期望规范 {id?, kind, target, expect}（与 spec_id / sample 互斥或任选一）",
            },
            "spec_id": {
                "type": "string",
                "description": "已存储规范的 ID（与 spec / sample 任选一）",
            },
            "sample": {
                "type": "object",
                "description": "正常成功请求/响应样本，用于免手写自动推导断言基线（可选）",
            },
            "trace_id": {
                "type": "string",
                "description": "关联的 trace_id（可选，写入结果便于关联）",
            },
        },
        "required": ["actual"],
    },
}


def verify_handler(arguments: dict) -> dict:
    """verify 工具处理函数。"""
    actual = arguments.get("actual") or {}
    spec = arguments.get("spec")
    spec_id = arguments.get("spec_id")
    sample = arguments.get("sample")
    trace_id = arguments.get("trace_id")

    # 1. 如果提供了 sample，自动免手写推导断言 spec
    if spec is None and not spec_id and sample:
        from app.runtime.verifier.spec_generator import infer_spec_from_sample
        target = arguments.get("target") or "inferred_target"
        spec = infer_spec_from_sample(target=target, sample=sample)

    # 2. spec 优先；没传 spec 则尝试用 spec_id 从存储取
    if spec is None and spec_id:
        spec = spec_store.get(spec_id)
        if spec is None:
            return {
                "matched": False,
                "diffs": [],
                "silent_failure": False,
                "error": f"spec_id '{spec_id}' not found",
            }

    if spec is None:
        return {
            "matched": False,
            "diffs": [],
            "silent_failure": False,
            "error": "must provide spec, spec_id, or sample",
        }

    # trace_id 透传到断言结果
    if trace_id:
        spec = dict(spec)
        spec["trace_id"] = trace_id

    result = assert_behavior(actual, spec)

    # 有 trace_id 时持久化结果，供 build_debug_context 注入 spec_diffs（V5 闭环）
    if trace_id:
        try:
            from app.runtime.core.logs import add_log
            add_log(trace_id, "verify", result)
        except Exception:
            pass

    # M1-B: 验证成功时写回 KB —— 递增 verify_count、提升 case_confidence。
    # 验证失败（matched=false）不写回，防止未验证的结论被误标为已验证。
    # 需要从 debug_context 提取 fingerprint 来命中 KB 条目。
    if result.get("matched") and trace_id:
        try:
            _try_kb_writeback(trace_id)
        except Exception:
            logger.warning("verify KB writeback failed", exc_info=True)

    return result


def _try_kb_writeback(trace_id: str) -> None:
    """验证成功后尝试写回 KB。

    从 debug_context 提取异常指纹，命中 KB 条目则递增 verify_count。
    未命中或无指纹时静默跳过（不阻断验证结论）。
    """
    from app.rag.knowledge_base import record_verification
    from app.runtime.context.builder import build_debug_context

    ctx = build_debug_context(trace_id)
    if ctx is None:
        return
    # DebugContext.exception 是 Optional[dict]，含 type/message/fingerprint
    exception = ctx.exception if hasattr(ctx, "exception") else None
    if not isinstance(exception, dict):
        return
    fingerprint = exception.get("fingerprint") or ""
    if not fingerprint:
        return
    record_verification(fingerprint, confidence=0.7)

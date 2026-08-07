"""
MCP 工具：verify —— 比对实际结果 vs 期望规范，自动检测静默失败。

输入 actual（实际结果）+ spec（期望规范）或 spec_id（已存储规范的 ID）。
输出 VerifyResult = {matched, diffs, silent_failure, spec_id?, trace_id?}

spec 与 spec_id 二选一：传 spec 直接比对；传 spec_id 从 spec_store 取已存储规范。
"""
from app.runtime.verifier.assert_engine import assert_behavior
from app.runtime.verifier import spec_store

VERIFY_DEF = {
    "name": "verify",
    "description": (
        "比对实际结果与期望规范，自动检测静默失败（返回正常但不符合规范）。"
        "传入 actual（实际结果）+ spec（期望规范）或 spec_id（已存储规范的 ID）。"
        "返回 {matched, diffs, silent_failure}。"
        "当 matched=false 且无异常、无 4xx/5xx 时，silent_failure=true。"
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
                "description": "期望规范 {id?, kind, target, expect}（与 spec_id 二选一）",
            },
            "spec_id": {
                "type": "string",
                "description": "已存储规范的 ID（与 spec 二选一）",
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
    trace_id = arguments.get("trace_id")

    # spec 优先；没传 spec 则尝试用 spec_id 从存储取
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
            "error": "must provide spec or spec_id",
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

    return result

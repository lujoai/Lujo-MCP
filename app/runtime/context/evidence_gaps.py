"""证据缺口提示（missing_evidence）—— 纯确定性判定，无 LLM、无 I/O。

目的：diagnose_issue 返回给宿主的调试上下文上注入可选字段
``missing_evidence``，告诉宿主 AI「这次现场还缺什么证据、建议怎么补」，
让它能主动补齐（调用对应工具 / 检查采集配置）而不是基于残缺现场硬猜。

注入点：diagnose_api._build_context（build_debug_context 产物 model_dump
之后）。不在 builder/schema 上扩字段——build_debug_context 的输出字段
契约由既有测试锁定，且 repair 链路等其它消费方不受影响。

口径（维度名与 app/quality/scorer.py 对齐）：
- 只判定 DebugContext 自身携带的证据维度：trace / code_snippet / runtime /
  git_context / network / ui_event / spec；
- knowledge_base / llm_analysis 属于修复链路（RepairContextAssembler 装配
  repair_context 时才有），不在 DebugContext 上判定；
- 每个维度仅当「真实证据存在」才算覆盖（network_trace 非空、git_blame 或
  recent_diffs 可得等），绝不凭空生成提示。

设计约束：
- 纯函数：只读输入 dict，无 I/O、无副作用、无 LLM 调用（满足 <50ms 预算）；
- fail-open：调用方（diagnose_api._build_context）以 try/except 包裹，
  本函数内部也做防御性处理，任何异常不得导致 diagnose_issue 整体失败；
- 向后兼容：missing_evidence 是返回 dict 上的可选新增字段，无缺失时为
  None（序列化为 null），旧客户端可安全忽略；
- 会话口径：提示文案不引入强制 session 维度（会话过滤始终是可选参数，
  见 docs/internal/CODE_REVIEW.md §2 产品定位）。
"""

from __future__ import annotations

from typing import Any, Callable

# 缺失维度 → 给宿主 AI 的可执行下一步提示。
# 文案只指向真实存在的 MCP 工具（stacktrace / get_network_trace /
# get_blame_for_frame / get_recent_diff / verify / ingest_specs）或采集配置。
_HINTS: dict[str, str] = {
    "trace": (
        "本次现场没有异常堆栈帧；若为前端报错，可调用 stacktrace 工具对堆栈"
        "做符号化解析，或在复现问题后重新调用 diagnose_issue"
    ),
    "code_snippet": (
        "未定位到源码片段（业务代码不在本服务可访问范围或路径映射失败）；"
        "可调用 stacktrace 查看堆栈帧详情，并确认业务代码位于 Lujo 工作目录内"
    ),
    "runtime": (
        "无运行时快照（采集被关闭或失败）；如需进程状态证据，可重新触发一次诊断"
    ),
    "git_context": (
        "git 归因不可用（非 git 仓库或文件路径不在白名单）；可对堆栈帧文件"
        "调用 get_blame_for_frame / get_recent_diff 手动补齐归因"
    ),
    "network": (
        "未采集到网络请求链；请确认浏览器 SDK 已启用且 endpoint 指向本服务，"
        "或调用 get_network_trace（trace_id=本次返回的 trace_id）按需查询"
    ),
    "ui_event": (
        "未采集到 UI 事件；请确认浏览器 SDK 已接入页面并上报用户操作，"
        "复现问题后重新调用 diagnose_issue"
    ),
    "spec": (
        "无规范校验结果；可调用 verify（必要时先用 ingest_specs 录入规范）"
        "对本次行为做断言闭环"
    ),
}


def _has_trace(debug_ctx: dict[str, Any]) -> bool:
    """异常 + 堆栈帧是否存在（口径对齐 scorer._score_trace_base）。"""
    exc = debug_ctx.get("exception")
    if not isinstance(exc, dict):
        return False
    frames = exc.get("frames")
    if isinstance(frames, list) and frames:
        return True
    frame_count = exc.get("frame_count")
    return isinstance(frame_count, int) and not isinstance(frame_count, bool) and frame_count > 0


def _has_code_snippet(debug_ctx: dict[str, Any]) -> bool:
    """源码片段是否成功采集（至少一帧 found=True）。"""
    snippets = debug_ctx.get("code_snippets")
    if not isinstance(snippets, list) or not snippets:
        return False
    return any(isinstance(s, dict) and s.get("found") for s in snippets)


def _has_runtime(debug_ctx: dict[str, Any]) -> bool:
    """运行时快照是否存在（真实结构为 runtime.process.pid）。"""
    runtime = debug_ctx.get("runtime")
    if not isinstance(runtime, dict):
        return False
    process = runtime.get("process")
    return isinstance(process, dict) and bool(process.get("pid"))


def _has_git_context(debug_ctx: dict[str, Any]) -> bool:
    """git blame 或 recent diff 任一可得即算覆盖（口径对齐 scorer）。"""
    return bool(debug_ctx.get("git_blame") or debug_ctx.get("recent_diffs"))


def _has_network(debug_ctx: dict[str, Any]) -> bool:
    return bool(debug_ctx.get("network_trace"))


def _has_ui_event(debug_ctx: dict[str, Any]) -> bool:
    return bool(debug_ctx.get("ui_events"))


def _has_spec(debug_ctx: dict[str, Any]) -> bool:
    """规范校验结果或相关规范引用任一存在即算覆盖。"""
    return bool(debug_ctx.get("spec_diffs") or debug_ctx.get("related_specs"))


# 维度 → 存在性判定；元组顺序即输出顺序（核心证据在前）
_DIMENSION_CHECKS: tuple[tuple[str, Callable[[dict], bool]], ...] = (
    ("trace", _has_trace),
    ("code_snippet", _has_code_snippet),
    ("runtime", _has_runtime),
    ("git_context", _has_git_context),
    ("network", _has_network),
    ("ui_event", _has_ui_event),
    ("spec", _has_spec),
)


def compute_missing_evidence(debug_ctx: dict[str, Any] | None) -> list[dict[str, str]]:
    """按证据真实存在性判定缺口，返回 ``[{"dimension", "hint"}, ...]``。

    输入为 build_debug_context 组装出的上下文 dict（与 DebugContext 同形状）。
    单维度判定异常时按「缺失」处理：宁可信其无——提示缺失的代价只是宿主
    多做一次确认，而误报「证据存在」会让宿主跳过补齐。
    """
    if not isinstance(debug_ctx, dict):
        return []
    missing: list[dict[str, str]] = []
    for dimension, check in _DIMENSION_CHECKS:
        try:
            if check(debug_ctx):
                continue
        except Exception:
            pass
        missing.append({"dimension": dimension, "hint": _HINTS[dimension]})
    return missing

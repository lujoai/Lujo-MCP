"""共享工具失败契约（FIX: R7）—— 统一 stdio 与 HTTP 的工具错误语义。

背景：业务 handler 此前把「执行失败」（未知工具 / 超时 / 内部异常 / 参数级
失败）捕获成 ``{"error": ...}`` dict 正常返回，传输层一律包装为
``isError=false`` 的成功结果——宿主智能体无法可靠识别失败并重试。

契约（对 handler 透明，不改变任何 handler 的返回结构）：
- handler 返回含非空 ``error`` 键的 dict  →  视为执行失败；
- 正常的「没有找到数据」（如 diagnose_issue 的 ``found=false``）不含
  ``error`` 键，仍视为成功返回，不被误判为错误。

FIX(R8) 契约边界：**结论型工具**（verify / verify_ui）不适用上面第一条。
它们的载荷本身就是「验证结论」（``matched`` / ``diffs`` / ``silent_failure``），
``error`` 只是原因说明（"must provide spec or spec_id"、"playwright 未安装…"）。
把结论标成 ``isError=true`` 会让宿主认为工具坏了并重试，而不是读这份结论
——包括「验证不通过」这种最有价值的结论。这类工具在注册时声明
``is_failure=conclusion_tool_is_failure``；真正的执行失败（Playwright 崩溃、
子进程超时）走异常路径，不经谓词，仍被传输层正确标记。

传输适配层据此统一处理：
- stdio（app/mcp_server.py）：raise :class:`ToolExecutionError`，
  官方 MCP SDK 会把它包装为 ``CallToolResult(isError=True)``；
- HTTP（app/mcp/protocol/server.py）：``make_response(..., isError=True)``。
两条传输都经 ``protocol.server.tool_failure_predicate(tool)`` 取谓词，
未声明的工具回落到下面的全局契约。
"""

from __future__ import annotations

# 结论型工具载荷的识别键：同时出现即认为这是一份「验证结论」而非失败报告。
_CONCLUSION_KEYS = ("matched", "diffs")


class ToolExecutionError(Exception):
    """工具执行失败。

    stdio 传输层必须抛出本异常（而非返回错误 dict），由官方 MCP SDK
    转换为 ``CallToolResult(isError=True)``；异常消息即返回给宿主的
    文本载荷（保持既有 JSON 结构，宿主仍可解析 error 字段）。
    """


def is_tool_failure_result(result: object) -> bool:
    """判定 handler 正常返回的结果是否代表执行失败。

    契约：dict 且含非空 ``error`` 键 ⇒ 失败；其余（含 found=false 等
    正常无数据结果）⇒ 成功。
    """
    return isinstance(result, dict) and bool(result.get("error"))


def is_conclusion_result(result: object) -> bool:
    """载荷是否为「验证结论」（matched/diffs 齐备）。"""
    return isinstance(result, dict) and all(key in result for key in _CONCLUSION_KEYS)


def conclusion_tool_is_failure(result: object) -> bool:
    """结论型工具（verify / verify_ui）的失败判定：结论不是失败。

    非结论形状的载荷（例如 handler 直接返回 ``{"error": ...}``）仍按全局契约
    判为失败，避免谓词变成"该工具永不失败"的口子。
    """
    if is_conclusion_result(result):
        return False
    return is_tool_failure_result(result)

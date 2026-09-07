"""共享工具失败契约（FIX: R7）—— 统一 stdio 与 HTTP 的工具错误语义。

背景：业务 handler 此前把「执行失败」（未知工具 / 超时 / 内部异常 / 参数级
失败）捕获成 ``{"error": ...}`` dict 正常返回，传输层一律包装为
``isError=false`` 的成功结果——宿主智能体无法可靠识别失败并重试。

契约（对 handler 透明，不改变任何 handler 的返回结构）：
- handler 返回含非空 ``error`` 键的 dict  →  视为执行失败；
- 正常的「没有找到数据」（如 diagnose_issue 的 ``found=false``）不含
  ``error`` 键，仍视为成功返回，不被误判为错误。

传输适配层据此统一处理：
- stdio（app/mcp_server.py）：raise :class:`ToolExecutionError`，
  官方 MCP SDK 会把它包装为 ``CallToolResult(isError=True)``；
- HTTP（app/mcp/protocol/server.py）：``make_response(..., isError=True)``。
"""

from __future__ import annotations


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

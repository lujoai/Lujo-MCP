"""ingest 系列工具的参数契约（P0-A，2026-09-27）。

Dogfooding 实证（lujo-dogfood-car-project）：必填语义字段静默默认会让宿主
AI 传错参数名时存入空记录并返回成功——静默假成功。此处统一改为显式拒绝，
错误消息列出正确参数名，由协议层转换为 ``isError=true`` 返回宿主。
HTTP SDK 路由不受影响（保留其对真实 SDK 上报的友好默认）。
"""
from __future__ import annotations

from app.mcp.protocol.tool_errors import ToolExecutionError


def require_text(arguments: dict, name: str, *, example: str = "") -> str:
    """取非空字符串参数；缺失或空白即抛 ToolExecutionError。"""
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        hint = f"（示例：{example}）" if example else ""
        raise ToolExecutionError(
            f"缺少必填参数 {name}：请以字符串提供{hint}。"
            f"收到的是 {value!r}——若参数名拼错请对照工具描述的 inputSchema。"
        )
    return value


def require_text_in_record(record: dict, name: str) -> str:
    """record 字典内的必填字符串字段。"""
    value = record.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(
            f"record.{name} 为必填字符串（如网络记录的目标 URL），收到的是 {value!r}"
        )
    return value

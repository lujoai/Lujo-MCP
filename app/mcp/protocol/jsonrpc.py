"""MCP 协议核心 —— JSON-RPC 2.0 消息解析与封装"""

import json
from typing import Any, Optional, Union
from pydantic import BaseModel


class JSONRPCRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Union[int, str]] = None
    method: str
    params: Optional[dict] = None


class JSONParseError(ValueError):
    """JSON 语法解析失败 → 对应 -32700"""


class InvalidRequestError(ValueError):
    """JSON 合法但不是合法 Request 对象 → 对应 -32600"""


# 标准 JSON-RPC 2.0 错误码 (-32768 到 -32000 为预留)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# 扩展/语义化应用级错误码 (-32000 到 -32099 为 JSON-RPC 预留服务器错误范围)
SERVER_ERROR_RESERVED_START = -32000
SERVER_ERROR_RESERVED_END = -32099
TOOL_EXECUTION_ERROR = -32000
TOOL_TIMEOUT_ERROR = -32001
RATE_LIMIT_ERROR = -32002
AUTH_ERROR = -32003
TOOL_BUSY_ERROR = -32004


def make_response(id: Optional[Union[int, str]], result: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": id,
        "result": result,
    }


def make_error(
    id: Optional[Union[int, str]],
    code: int,
    message: str,
    data: Optional[Any] = None,
) -> dict:
    """构建符合 JSON-RPC 2.0 规范的 Error 响应对象，可选携带 data 附加信息"""
    err = {
        "code": code,
        "message": message,
    }
    if data is not None:
        err["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": id,
        "error": err,
    }


def parse_request(raw: str | bytes) -> JSONRPCRequest:
    """解析原始 JSON-RPC 请求，保留 id 的原始类型（int/str/None）。

    使用 model_construct 跳过 Pydantic 验证，避免 Union[int, str] 把字符串 id 强转为 int。
    JSON-RPC 2.0 规范允许 id 为 String、Number 或 NULL。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise JSONParseError("Invalid JSON")

    if not isinstance(data, dict):
        raise InvalidRequestError("请求必须是 JSON 对象")

    if "method" not in data:
        raise InvalidRequestError("缺少 method 字段")

    return JSONRPCRequest.model_construct(
        jsonrpc=data.get("jsonrpc", "2.0"),
        id=data.get("id"),
        method=data["method"],
        params=data.get("params"),
    )

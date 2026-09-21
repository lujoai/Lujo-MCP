"""MCP 协议核心 —— JSON-RPC 2.0 消息解析与封装"""

import json
import math
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
#
# ⚠️ 使用现状（W11 / P3-PRO-4 核对，勿再当成"全都在用"或"全是死代码"）：
# - **AUTH_ERROR 已接线**：HTTP 侧 RBAC 角色不足时返回它（`app/api/mcp_routes.py`，
#   HTTP 403）。此前该分支误用 INVALID_REQUEST(-32600)，语义是"请求结构非法"。
# - 其余四个（TOOL_EXECUTION / TOOL_TIMEOUT / RATE_LIMIT / TOOL_BUSY）**是有意
#   保留、当前无生产消费者**：工具级失败不走 JSON-RPC 顶层 error，而是按 MCP
#   规范返回 `result.isError = true` + 载荷内的**字符串** `error_code`
#   （`TOOL_BUSY` / `TOOL_TIMEOUT` / `TOOL_INTERNAL`，单一声明在
#   `app/mcp/protocol/tool_errors.py` 的 `MCP_TOOL_ERROR_CODES`，两传输同源）。
#   限流同样在 HTTP 层用 429 表达。宿主判定工具失败一律以 `isError` +
#   `result.error_code` 为准，不要等这些数字码（对外文档见
#   `docs/public/API_REFERENCE.md` §3.4/§3.4.1）。
#   删除它们需要同步删 `tests/unit/test_jsonrpc.py` 的常量与区间断言，而保留
#   的代价只是四个常量 —— 按"不为风格删有测试锁定的声明"处置，保留并在此说明。
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
        # FIX(v0.7.1-b4-2): 非法 UTF-8 字节此前抛 UnicodeDecodeError（非
        # -32700 语义）；HTTP 等调用方依赖 JSONParseError 归一到 PARSE_ERROR。
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise JSONParseError(f"Invalid UTF-8: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise JSONParseError("Invalid JSON")

    if not isinstance(data, dict):
        raise InvalidRequestError("请求必须是 JSON 对象")

    if "method" not in data:
        raise InvalidRequestError("缺少 method 字段")

    # FIX: v0.6.6 错误 method —— 非 str 的 method（list/dict 等）会经
    # model_construct 原样透传，dispatch 路由时 _METHOD_MAP.get(不可哈希对象)
    # 抛 TypeError 且该查找位于 dispatch 的 try 之外，错误码退化为 500/-32603。
    if not isinstance(data["method"], str):
        raise InvalidRequestError("method 必须为字符串")

    # FIX(v0.7.1-b3-6): jsonrpc 字段值必须为 "2.0"（协议版本号）——
    # 此前非 "2.0"/非字符串值原样透传（model_construct 跳过校验），
    # 下游按 2.0 语义处理不兼容协议声明。仅版本字段值不一致按 -32600
    # 拒绝；缺省时与旧行为一致默认 "2.0"（宽容无版本号的纯 JSON 客户端）。
    if data.get("jsonrpc", "2.0") != "2.0":
        raise InvalidRequestError("jsonrpc 必须为 '2.0'")

    # FIX: v0.6.6 错误 id —— id 必须为 String/Number/NULL；dict/list/bool 会被
    # model_construct 原样透传并回显到响应，NaN/Infinity 更会产出非法 JSON
    # （{"id": NaN}）。此处前置校验，坏 id 按 -32600 返回且响应 id 为 null。
    req_id = data.get("id")
    if req_id is not None and (
        isinstance(req_id, bool) or not isinstance(req_id, (str, int, float))
    ):
        raise InvalidRequestError("id 必须为字符串、数字或 null")
    if isinstance(req_id, float) and not math.isfinite(req_id):
        raise InvalidRequestError("id 不能为 NaN 或 Infinity")

    return JSONRPCRequest.model_construct(
        jsonrpc=data.get("jsonrpc", "2.0"),
        id=req_id,
        method=data["method"],
        params=data.get("params"),
    )

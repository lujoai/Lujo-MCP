"""MCP 协议服务器 —— 遵循 MCP 2024-11-05 规范"""

import json
import logging
from typing import Any

from app.config import settings
from app.mcp.protocol.jsonrpc import (
    JSONRPCRequest,
    make_response,
    make_error,
    METHOD_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_PARAMS,
)

logger = logging.getLogger("ai-debug-mcp.protocol")

# MCP 协议版本
PROTOCOL_VERSION = "2024-11-05"

# 服务端能力声明
CAPABILITIES = {
    "tools": {},  # 支持工具调用
    "resources": {},  # 保留扩展
}

SERVER_INFO = {
    "name": settings.service_name,
    "version": "0.2.0",
}

# 工具注册表
_tool_registry: dict[str, dict] = {}


def register_tool(
    name: str,
    description: str,
    handler: callable,
    **kwargs,
):
    """注册一个 MCP 工具。inputSchema 等额外字段通过 kwargs 透传。"""
    _tool_registry[name] = {
        "name": name,
        "description": description,
        "inputSchema": kwargs.get("inputSchema", {}),
        "handler": handler,
    }


def _handle_initialize(req: JSONRPCRequest) -> dict:
    """处理 initialize 握手"""
    return make_response(req.id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": CAPABILITIES,
        "serverInfo": SERVER_INFO,
    })


def _handle_tools_list(req: JSONRPCRequest) -> dict:
    """处理 tools/list"""
    tools = [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in _tool_registry.values()
    ]
    return make_response(req.id, {"tools": tools})


async def _handle_tools_call(req: JSONRPCRequest) -> dict:
    """处理 tools/call"""
    params = req.params or {}
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    tool = _tool_registry.get(tool_name)
    if not tool:
        return make_error(req.id, METHOD_NOT_FOUND, f"未知工具: {tool_name}")

    try:
        result = tool["handler"](arguments)
        if __import__("asyncio").iscoroutine(result):
            result = await result
        return make_response(req.id, {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, default=str),
                }
            ],
            "isError": False,
        })
    except Exception as e:
        logger.exception(f"工具 {tool_name} 执行失败")
        return make_response(req.id, {
            "content": [
                {
                    "type": "text",
                    "text": "工具执行失败，详情见服务端日志",
                }
            ],
            "isError": True,
        })


def _handle_ping(req: JSONRPCRequest) -> dict:
    """处理 ping"""
    return make_response(req.id, {})


# 方法路由表
_METHOD_MAP = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": _handle_ping,
}


async def dispatch(jsonrpc_request: JSONRPCRequest) -> dict:
    """
    路由 JSON-RPC 请求到对应处理方法

    输入: JSONRPCRequest
    输出: JSON-RPC Response dict（可直接序列化返回）
    """
    method = jsonrpc_request.method
    handler = _METHOD_MAP.get(method)

    if handler is None:
        return make_error(
            jsonrpc_request.id,
            METHOD_NOT_FOUND,
            f"未支持的方法: {method}",
        )

    try:
        result = handler(jsonrpc_request)
        if __import__("asyncio").iscoroutine(result):
            result = await result
        return result
    except Exception:
        logger.exception(f"处理 {method} 时出错")
        return make_error(
            jsonrpc_request.id,
            INTERNAL_ERROR,
            "内部错误，详情见服务端日志",
        )


async def dispatch_raw(raw: str | bytes) -> dict:
    """解析原始请求文本并分发"""
    from app.mcp.protocol.jsonrpc import parse_request

    try:
        req = parse_request(raw)
    except (ValueError, TypeError) as e:
        return make_error(None, INVALID_PARAMS, str(e))

    return await dispatch(req)

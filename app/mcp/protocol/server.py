"""MCP 协议服务器 —— 遵循 MCP 2024-11-05 规范"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app import __version__
from app.config import settings
from app.mcp.protocol.jsonrpc import (
    JSONRPCRequest,
    make_response,
    make_error,
    METHOD_NOT_FOUND,
    INTERNAL_ERROR,
    PARSE_ERROR,
    INVALID_REQUEST,
    INVALID_PARAMS,
    JSONParseError,
    InvalidRequestError,
    parse_request,
)

logger = logging.getLogger("lujo-mcp.protocol")

# MCP 协议版本
PROTOCOL_VERSION = "2024-11-05"

# 服务端支持的协议版本列表（用于版本协商，按推荐度降序）
SUPPORTED_PROTOCOL_VERSIONS = ["2024-11-05", "2024-08-27"]

# 服务端能力声明
CAPABILITIES = {
    "tools": {},  # 支持工具调用
}

SERVER_INFO = {
    "name": settings.service_name,
    "version": __version__,
}

# 工具注册表
_tool_registry: dict[str, dict] = {}

# FIX P3-12: 同步工具 handler 专用有界线程池（与 app/mcp_server.py 同方案）。
# 避免超时 handler 占用 asyncio 默认线程池并拖累其它 to_thread 任务；池有界不无限增长。
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=settings.tool_executor_workers)
_tool_slots = asyncio.Semaphore(settings.tool_executor_workers)


def register_tool(
    name: str,
    description: str,
    handler: callable,
    **kwargs,
):
    """注册一个 MCP 工具。inputSchema 等额外字段通过 kwargs 透传。

    v0.5: 支持 category 和 experimental 元数据（可选，向后兼容）。
    """
    _tool_registry[name] = {
        "name": name,
        "description": description,
        "inputSchema": kwargs.get("inputSchema", {}),
        "handler": handler,
        "category": kwargs.get("category"),
        "experimental": kwargs.get("experimental", False),
    }


def _handle_initialize(req: JSONRPCRequest) -> dict:
    """处理 initialize 握手 —— 协商协议版本

    读取客户端 params.protocolVersion：
    - 若在 SUPPORTED_PROTOCOL_VERSIONS 中则回显该版本
    - 未知/缺失版本回退到 PROTOCOL_VERSION 并记录 warning
    """
    params = req.params or {}
    client_version = params.get("protocolVersion")

    if client_version and client_version in SUPPORTED_PROTOCOL_VERSIONS:
        # 客户端请求的版本在支持列表内，回显该版本
        negotiated = client_version
    else:
        # 未知/缺失版本，回退到服务端最新版本并记录 warning
        if client_version:
            logger.warning(
                "客户端请求的协议版本 %s 不在支持列表 %s 中，回退到 %s",
                client_version, SUPPORTED_PROTOCOL_VERSIONS, PROTOCOL_VERSION,
            )
        else:
            logger.warning("客户端未提供 protocolVersion，回退到 %s", PROTOCOL_VERSION)
        negotiated = PROTOCOL_VERSION

    return make_response(req.id, {
        "protocolVersion": negotiated,
        "capabilities": CAPABILITIES,
        "serverInfo": SERVER_INFO,
    })


def _handle_tools_list(req: JSONRPCRequest) -> dict:
    """处理 tools/list

    v0.5: 响应中包含 category 和 experimental 元数据。
    旧 MCP 客户端可忽略这些额外字段（JSON 语义安全）。
    """
    tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["inputSchema"],
            "category": t.get("category"),
            "experimental": t.get("experimental", False),
        }
        for t in _tool_registry.values()
    ]
    return make_response(req.id, {"tools": tools})


async def _handle_tools_call(req: JSONRPCRequest) -> dict:
    """处理 tools/call"""
    # FIX: P1-9i params 非 dict（list/str/null）时返回 -32602，避免 AttributeError → 500
    if not isinstance(req.params, dict):
        return make_error(req.id, INVALID_PARAMS, "Invalid params")
    params = req.params
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    tool = _tool_registry.get(tool_name)
    if not tool:
        return make_error(req.id, METHOD_NOT_FOUND, f"未知工具: {tool_name}")

    timeout = settings.tool_timeout_seconds
    _tool_start = time.monotonic()
    handler = tool["handler"]

    if asyncio.iscoroutinefunction(handler):
        try:
            result = await asyncio.wait_for(handler(arguments), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("工具 %s 执行超时（>%ss），已中止", tool_name, timeout)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": f"工具执行超时（>{timeout}s），已中止。",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_TIMEOUT",
                "_timed_out": True,
            })
        except Exception:
            logger.exception("工具 %s 执行失败", tool_name)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": "工具执行失败，详情见服务端日志",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_INTERNAL",
            })
    else:
        # 同步工具：获取并发槽位（带等待超时，防止线程池排队堆积与饿死）
        busy_timeout = settings.tool_busy_queue_timeout
        try:
            await asyncio.wait_for(_tool_slots.acquire(), timeout=busy_timeout)
        except asyncio.TimeoutError:
            if busy_timeout <= 0:
                logger.warning("工具 %s 执行队列已满（不等待，立即拒绝），已拒绝执行", tool_name)
            else:
                logger.warning("工具 %s 执行队列已满（等待 %.3fs 后超时），已拒绝执行", tool_name, busy_timeout)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": "工具执行队列已满，请稍后重试。",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_BUSY",
                "_busy": True,
            })

        sync_future: asyncio.Future | None = None
        try:
            loop = asyncio.get_running_loop()
            sync_future = loop.run_in_executor(_TOOL_EXECUTOR, handler, arguments)
            result = await asyncio.wait_for(sync_future, timeout=timeout)
        except asyncio.TimeoutError:
            if sync_future is not None:
                # 仅在线程尚未启动排队中时 cancel() 有效；
                # 线程一旦已启动，Python threading 模型下 cancel() 无法中断正在执行的线程，
                # 线程仍会跑完，这是有界线程池设计下的已知限制。
                sync_future.cancel()
            logger.warning("工具 %s 执行超时（>%ss），已中止", tool_name, timeout)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": f"工具执行超时（>{timeout}s），已中止。",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_TIMEOUT",
                "_timed_out": True,
            })
        except Exception:
            logger.exception("工具 %s 执行失败", tool_name)
            return make_response(req.id, {
                "content": [
                    {
                        "type": "text",
                        "text": "工具执行失败，详情见服务端日志",
                    }
                ],
                "isError": True,
                "error_code": "TOOL_INTERNAL",
            })
        finally:
            _tool_slots.release()

    _elapsed = time.monotonic() - _tool_start
    try:
        _size = len(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        _size = 0
    # Phase 3 D5：记录 Tool 响应耗时/大小（仅日志，不修改协议响应、不打印敏感负载）
    logger.info("MCP HTTP tool=%s response_ms=%.1f response_size=%d", tool_name, _elapsed * 1000, _size)
    return make_response(req.id, {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, default=str),
            }
        ],
        "isError": False,
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
        if asyncio.iscoroutine(result):
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
    try:
        req = parse_request(raw)
    except JSONParseError as e:
        return make_error(None, PARSE_ERROR, str(e))
    except InvalidRequestError as e:
        return make_error(None, INVALID_REQUEST, str(e))

    return await dispatch(req)

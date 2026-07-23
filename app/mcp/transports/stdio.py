"""MCP stdio 传输 —— 供 Claude Desktop 等本地客户端以子进程方式连接

协议：stdin 逐行读取 JSON-RPC 请求，stdout 逐行写回 JSON-RPC 响应。
通知（无 id）不写回响应体。
注意：stdin/stdout 是协议通道，日志必须走 stderr，否则会污染协议流。
"""
import sys
import json
import logging
import asyncio

from app.mcp.tools import register_all_tools
from app.mcp.protocol.server import dispatch
from app.mcp.protocol.jsonrpc import parse_request
from app.mcp.protocol.jsonrpc import (
    make_error,
    INVALID_REQUEST,
    INTERNAL_ERROR,
    PARSE_ERROR,
    JSONParseError,
    InvalidRequestError,
)

logger = logging.getLogger("ai-debug-mcp.mcp.stdio")


def _configure_stdio_logging():
    """日志重定向到 stderr，避免污染 stdout 协议通道"""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _write_response(response: dict):
    """安全写入 JSON-RPC 响应到 stdout"""
    try:
        payload = json.dumps(response, ensure_ascii=False, default=str)
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except Exception as e:
        logger.error("写入 stdout 失败: %s", e)


async def run_stdio():
    _configure_stdio_logging()
    register_all_tools()
    logger.info("MCP stdio server 启动，等待 stdin 输入")

    # 注册资源清理兜底（与 app.mcp_server.main 一致：atexit + finally）
    from app.mcp_server import cleanup_resources
    import atexit
    atexit.register(cleanup_resources)

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except EOFError:
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = parse_request(line)
            except JSONParseError as e:
                _write_response(
                    make_error(None, PARSE_ERROR, str(e))
                )
                continue
            except InvalidRequestError as e:
                _write_response(
                    make_error(None, INVALID_REQUEST, str(e))
                )
                continue

            try:
                result = await dispatch(req)
            except Exception:
                logger.exception("dispatch 执行异常")
                _write_response(
                    make_error(req.id, INTERNAL_ERROR, "内部错误，详情见服务端日志")
                )
                continue

            # 通知类消息（无 id）不写回响应
            if req.id is None:
                continue

            _write_response(result)
    finally:
        logger.info("MCP stdio server 关闭（stdin 关闭）")
        cleanup_resources()


if __name__ == "__main__":
    asyncio.run(run_stdio())

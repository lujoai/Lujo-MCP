"""
轻量 MCP stdio 入口 —— 只加载 MCP 工具，不启动 FastAPI。

供 Qoder / Claude Desktop 等 MCP 客户端连接。
"""
import sys
import json
import logging
import asyncio

# 最小化导入：只注册 MCP 工具，不加载 FastAPI
from app.mcp.tools import register_all_tools
from app.mcp.protocol.server import dispatch
from app.mcp.protocol.jsonrpc import parse_request, make_error, INVALID_REQUEST, INTERNAL_ERROR

register_all_tools()

# 日志全走 stderr
logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
logger = logging.getLogger("ai-debug-mcp.stdio")


def _write_response(response: dict):
    """安全写入 JSON-RPC 响应到 stdout"""
    try:
        payload = json.dumps(response, ensure_ascii=False, default=str)
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except Exception as e:
        logger.error("写入 stdout 失败: %s", e)


async def main():
    logger.info("MCP stdio server 启动，等待 stdin 输入")
    loop = asyncio.get_event_loop()
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
        except Exception as e:
            _write_response(
                make_error(None, INVALID_REQUEST, str(e))
            )
            continue

        try:
            result = await dispatch(req)
        except Exception as e:
            logger.exception("dispatch 执行异常")
            _write_response(
                make_error(req.id, INTERNAL_ERROR, f"内部错误: {e}")
            )
            continue

        if req.id is None:
            continue
        _write_response(result)

    logger.info("MCP stdio server 关闭（stdin 关闭）")


if __name__ == "__main__":
    asyncio.run(main())

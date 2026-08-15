"""
标准 MCP Server（stdio transport）。

这是 Trae / Codex / Claude Desktop 之类的 MCP 客户端真正会启动的入口，
通过 stdio 管道 + JSON-RPC 协议通信（由 mcp SDK 处理，不需要自己实现协议细节）。

注册方式（在 Trae/Codex 的 MCP 配置里）：
{
  "mcpServers": {
    "lujo-mcp": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/绝对路径/lujo-mcp"
    }
  }
}

设计原则：这里只暴露"采集数据"的工具（get_stacktrace / get_debug_context /
get_runtime_snapshot / search_logs / list_recent_traces），
不默认做LLM推理 —— 宿主AI（Trae/Codex里的模型）拿到原始数据后自己判断根因，
这样避免重复推理、重复花钱。analyze_with_llm 作为可选工具保留，
仅在宿主客户端本身不具备推理能力时才需要用它。
"""
import asyncio
import atexit
import json
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from app.config import settings
from app import __version__
from app.runtime.hooks.exception_hook import install_global_hook, uninstall_global_hook
from app.mcp.protocol.server import _tool_registry
from app.mcp.tools import register_all_tools

logging.basicConfig(level=logging.INFO, stream=None, force=True)  # stdio模式下不要往stdout打日志，避免污染协议流
logger = logging.getLogger("lujo-mcp")

register_all_tools()
server = Server("lujo-mcp", version=__version__)

# ── stdio 生命周期资源回收 ──
# 由 finally / atexit / signal handler 触发，幂等。
_cleanup_done = False
_periodic_cleanup_task: asyncio.Task | None = None
_cleanup_lock = threading.Lock()


def cleanup_resources() -> None:
    """stdio 退出路径统一资源回收。

    幂等：多次调用（finally / atexit / signal）只执行一次。
    回收内容：
      1) 取消 periodic_cleanup 后台任务（若存在；当前 stdio 未启动，预留兜底）
      2) 关闭 PG 连接池（仅当 storage_backend == "postgresql"）
      3) 卸载全局 excepthook
    """
    global _cleanup_done, _periodic_cleanup_task
    with _cleanup_lock:
        if _cleanup_done:
            return
        _cleanup_done = True

        # 1) 取消后台 periodic_cleanup（防御性：当前 stdio 未启动该任务）
        task = _periodic_cleanup_task
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception as e:
                logger.warning(f"stdio 退出取消 periodic_cleanup 失败: {e}")

    # 2) 关闭 PG 连接池（仅 postgresql 后端）
    if settings.storage_backend == "postgresql":
        try:
            from app.runtime.core.storage.pg_store import close_pool
            close_pool()
        except Exception as e:
            logger.warning(f"stdio 退出关闭 PG 连接池失败: {e}")

    # 3) 卸载全局 excepthook
    try:
        uninstall_global_hook()
    except Exception as e:
        logger.warning(f"stdio 退出卸载 excepthook 失败: {e}")


def _signal_handler(signum, frame):
    """SIGINT/SIGTERM 兜底：触发清理后退出。

    在 asyncio 主循环运行时被调用，sys.exit(0) 抛 SystemExit，
    会被 asyncio 捕获并终止主循环，finally 仍会执行 cleanup_resources（幂等）。
    """
    try:
        cleanup_resources()
    except Exception:
        pass
    sys.exit(0)


def _register_signal_handlers() -> None:
    """注册 SIGINT/SIGTERM 兜底 handler。

    仅在主线程注册（signal 模块限制）。
    Windows 不支持 SIGTERM，try/except 保护。
    """
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGINT, _signal_handler)
    except (ValueError, OSError) as e:
        logger.warning(f"注册 SIGINT handler 失败: {e}")
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (AttributeError, ValueError, OSError):
        # Windows 无 SIGTERM；某些环境下不能注册
        pass


async def _run_registered_tool(handler: Callable, arguments: dict):
    timeout = settings.tool_timeout_seconds
    if asyncio.iscoroutinefunction(handler):
        return await asyncio.wait_for(handler(arguments), timeout=timeout)
    return await asyncio.wait_for(asyncio.to_thread(handler, arguments), timeout=timeout)


@server.list_tools()
async def list_tools() -> list[Tool]:#async声明函数是可以等待的
    return [
        Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool["inputSchema"],
            category=tool.get("category"),
            experimental=tool.get("experimental", False),
        )
        for tool in _tool_registry.values()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    _tool_start = time.monotonic()
    try:
        tool = _tool_registry.get(name)
        if tool is None:
            result = {"error": f"未知工具: {name}"}
        else:
            result = await _run_registered_tool(tool["handler"], arguments)
    except asyncio.TimeoutError:
        logger.warning("工具 %s 执行超时（>%ss），已中止", name, settings.tool_timeout_seconds)
        result = {"error": f"工具执行超时（>{settings.tool_timeout_seconds}s），已中止。", "_timed_out": True}
    except Exception as e:
        logger.error(str(e), exc_info=True)
        result = {"error": "Tool execution failed"}

    # Phase 3 D5：记录 Tool 响应耗时（仅日志，不修改协议响应、不打印敏感负载）
    _elapsed = time.monotonic() - _tool_start
    try:
        _size = len(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        _size = 0
    logger.info(
        "MCP tool=%s response_ms=%.1f response_size=%d",
        name, _elapsed * 1000, _size,
    )

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def main():
    install_global_hook()
    # 兜底1：atexit 注册（覆盖正常解释器退出路径）
    atexit.register(cleanup_resources)
    # 兜底2：signal handler（SIGINT/SIGTERM；Windows 不支持 SIGTERM，try/except 保护）
    _register_signal_handlers()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        # 正常 EOF / 协议退出路径
        cleanup_resources()


if __name__ == "__main__":
    asyncio.run(main())

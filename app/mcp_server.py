"""
标准 MCP Server（默认 stdio；可选统一本地 stdio + HTTP transport）。

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

传入 ``--http`` 时，当前进程还会在回环地址启动 FastAPI HTTP（默认
``127.0.0.1:8000``），让 Browser SDK 的 ``/ingest`` 上报与 stdio MCP
共享同一套存储；``--no-http`` 明确退回纯 stdio。
"""
import asyncio
import argparse
import atexit
from contextlib import suppress
import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from app.config import settings
from app import __version__
from app.runtime.hooks.exception_hook import install_global_hook, uninstall_global_hook
from app.mcp.protocol.server import _tool_registry, get_agent_visible_tools, is_heavy_tool
from app.mcp.protocol.heavy_process import run_heavy_tool_blocking
from app.mcp.protocol.tool_errors import ToolExecutionError, is_tool_failure_result
from app.mcp.tools import register_all_tools

logging.basicConfig(level=logging.INFO, stream=None, force=True)  # stdio模式下不要往stdout打日志，避免污染协议流
logger = logging.getLogger("lujo-mcp")

register_all_tools()
server = Server("lujo-mcp", version=__version__)

# FIX P3-12: 同步工具 handler 专用有界线程池。
# asyncio.to_thread 用默认线程池，工具超时后 handler 线程仍在池内继续跑，
# 反复超时会占满默认池 worker 并拖累其它 to_thread 任务。改用本专用池：
# - 超时后线程仍运行，但不占用默认池；
# - 池有界不会因反复超时无限增长。
# ThreadPoolExecutor 线程懒创建（首次 submit 才起线程），import 时创建无副作用。
# FIX(v0.7.1-b4-9): 池大小改由配置驱动（此前硬编码 8，与 protocol/server.py
# 的 _LIGHT_TOOL_EXECUTOR 用 settings.tool_executor_workers 的口径不一致，
# 配置无法调优 HTTP 侧并发）。
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=settings.tool_executor_workers)
_executor_lock = threading.Lock()


def _get_tool_executor() -> ThreadPoolExecutor:
    """获取同步工具专用线程池；已被 shutdown 时重建同规格的有界池。

    cleanup_resources 的 R7-A5 退出语义不变（退出路径仍 shutdown 当前池）；
    本函数只是保证 shutdown 之后的同一进程内模块仍可用（测试或长驻进程
    再次调用时自愈），否则一次退出清理就永久毒化后续所有工具调用。
    """
    global _TOOL_EXECUTOR
    with _executor_lock:
        if _TOOL_EXECUTOR._shutdown:
            _TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=settings.tool_executor_workers)
        return _TOOL_EXECUTOR

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
            from app.runtime.core.storage.pg_executor import close_pool
            close_pool()
        except Exception as e:
            logger.warning(f"stdio 退出关闭 PG 连接池失败: {e}")

    # 3) 卸载全局 excepthook
    try:
        uninstall_global_hook()
    except Exception as e:
        logger.warning(f"stdio 退出卸载 excepthook 失败: {e}")

    # 4) FIX: R7-A5 —— 关闭同步工具专用线程池。ThreadPoolExecutor 非 daemon，
    # 此前退出从不 shutdown：超时仍在跑的工具线程在解释器退出时被
    # concurrent.futures 的 _python_exit join → 进程无法退出直至宿主强杀。
    # wait=False 不等运行中任务；cancel_futures 撤掉排队未启动的任务。
    try:
        _TOOL_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception as e:
        logger.warning(f"stdio 退出关闭工具线程池失败: {e}")


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


def _parse_runtime_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析启动器参数，同时容忍 MCP 客户端附带的未知参数。

    默认保持历史行为：只启动 stdio MCP。传入 ``--http`` 后，入口会在
    同一事件循环中并行提供 stdio 与本机 HTTP；``--no-http`` 可供 npm
    启动器或发布冒烟显式退回纯 stdio。
    """
    parser = argparse.ArgumentParser(add_help=True, description="Lujo-MCP local MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="在同一进程中同时提供 MCP stdio 与 localhost HTTP（默认关闭）",
    )
    parser.add_argument(
        "--no-http",
        action="store_true",
        help="显式关闭 HTTP（用于回退到纯 stdio）",
    )
    parser.add_argument(
        "--http-host",
        default=None,
        help="HTTP 绑定地址（统一模式默认 127.0.0.1）",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="HTTP 端口（默认沿用 PORT，通常为 8000）",
    )
    # parse_known_args 是刻意的：某些 MCP 启动器会把额外参数传给命令，
    # 不应因为一个无关参数让已能工作的 stdio 服务无法启动。
    options, _unknown = parser.parse_known_args(argv)
    if options.http_port is not None and not 1 <= options.http_port <= 65535:
        parser.error("--http-port 必须在 1 到 65535 之间")
    return options


async def _run_stdio_transport() -> None:
    """运行官方 MCP stdio transport。单独抽出以便统一模式复用。"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def _run_unified_transport(host: str, port: int) -> None:
    """在一个进程内并行运行 stdio MCP 与 FastAPI HTTP。

    两个 transport 共享同一套工具注册表和内存存储，因此浏览器 SDK 通过
    ``/ingest`` 写入的数据可以立即被 stdio MCP 客户端读取。任一 transport
    结束都会有序停止另一个，避免 stdio EOF 后留下孤儿 HTTP 进程。
    """
    # 延迟导入 HTTP app，保证默认纯 stdio 启动不引入 FastAPI 生命周期或
    # 额外副作用，也让 PyInstaller 的 stdio 启动路径保持向后兼容。
    from app.main import app
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="on",
        log_config=None,
        access_log=False,
    )
    http_server = uvicorn.Server(config)
    logger.info("本地 HTTP 已启用: http://%s:%d（MCP: /mcp，采集: /ingest）", host, port)

    http_task = asyncio.create_task(http_server.serve(), name="lujo-http")
    stdio_task = asyncio.create_task(_run_stdio_transport(), name="lujo-stdio")
    try:
        done, _pending = await asyncio.wait(
            {http_task, stdio_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # HTTP 意外退出时，把错误抛给入口；否则 stdio 客户端会误以为服务
        # 仍在运行，但浏览器采集其实已经失效。
        if http_task in done and not http_task.cancelled():
            http_error = http_task.exception()
            if http_error is not None:
                stdio_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stdio_task
                raise http_error

        # stdio EOF 是 MCP 客户端正常退出路径。通知 Uvicorn 优雅关闭，触发
        # FastAPI lifespan 的清理逻辑（PG/Redis/后台任务等）。
        if stdio_task in done:
            stdio_error = None if stdio_task.cancelled() else stdio_task.exception()
            http_server.should_exit = True
            await http_task
            if stdio_error is not None:
                raise stdio_error
        else:
            # HTTP 正常停止（例如收到 SIGINT）时不让 stdio 协程悬挂。
            stdio_task.cancel()
            with suppress(asyncio.CancelledError):
                await stdio_task
    finally:
        # 双重兜底：异常或取消时也必须让两个任务结束，不能把后台 HTTP
        # 监听器遗留到解释器退出之后。
        http_server.should_exit = True
        if not http_task.done():
            with suppress(asyncio.CancelledError):
                await http_task
        if not stdio_task.done():
            stdio_task.cancel()
        with suppress(asyncio.CancelledError):
            await stdio_task


async def _run_registered_tool(name: str, tool: dict, arguments: dict):
    timeout = settings.tool_timeout_seconds
    handler = tool["handler"]
    if asyncio.iscoroutinefunction(handler):
        return await asyncio.wait_for(handler(arguments), timeout=timeout)

    # FIX: C2 —— 重型同步工具（如 verify_ui）改在子进程执行 + 超时 terminate()
    # 强杀（进程可杀，无僵尸）；父进程内存态入参先经 prepare_args 预处理。
    if is_heavy_tool(name):
        prepare = tool.get("prepare_args")
        if prepare is not None:
            try:
                arguments = prepare(arguments)
            except Exception:
                logger.warning("工具 %s prepare_args 失败，沿用原入参", name, exc_info=True)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            run_heavy_tool_blocking,
            handler.__module__,
            handler.__name__,
            arguments,
            float(timeout),
        )

    # FIX P3-12: 轻量同步 handler 走专用有界线程池 _TOOL_EXECUTOR，不占默认池；
    # 超时只取消 await，线程继续运行但池有界不增长。
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_get_tool_executor(), handler, arguments),
        timeout=timeout,
    )


@server.list_tools()
async def list_tools() -> list[Tool]:#async声明函数是可以等待的
    # v0.7.3: 与 HTTP 侧 _handle_tools_list 保持一致——只暴露 Agent-facing
    # 工具（SDK 上报类 agent_visible=False 不进清单，tools/call 仍可按名调用）
    return [
        Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool["inputSchema"],
            category=tool.get("category"),
            experimental=tool.get("experimental", False),
        )
        for tool in get_agent_visible_tools()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """执行工具并返回文本内容。

    FIX: R7 —— 失败必须以异常上抛（官方 MCP SDK 将其包装为
    ``CallToolResult(isError=True)``），而不是包装成含 error 字段的
    "成功"文本结果：宿主智能体依赖 isError 识别失败并重试。
    保持既有 JSON 载荷结构不变，仅修正外层失败标记。
    """
    _tool_start = time.monotonic()
    try:
        tool = _tool_registry.get(name)
        if tool is None:
            raise ToolExecutionError(json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False))
        result = await _run_registered_tool(name, tool, arguments)
        if is_tool_failure_result(result):
            raise ToolExecutionError(json.dumps(result, ensure_ascii=False, indent=2))
    except asyncio.TimeoutError:
        logger.warning("工具 %s 执行超时（>%ss），已中止", name, settings.tool_timeout_seconds)
        raise ToolExecutionError(json.dumps(
            {"error": f"工具执行超时（>{settings.tool_timeout_seconds}s），已中止。", "_timed_out": True},
            ensure_ascii=False,
        ))
    except ToolExecutionError:
        raise
    except Exception as e:
        logger.error(str(e), exc_info=True)
        raise ToolExecutionError(json.dumps({"error": "Tool execution failed"}, ensure_ascii=False))

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


async def main(argv: list[str] | None = None):
    options = _parse_runtime_args(argv)
    unified = options.http and not options.no_http
    install_global_hook()
    # 兜底1：atexit 注册（覆盖正常解释器退出路径）
    atexit.register(cleanup_resources)
    # 统一模式交给 Uvicorn 捕获信号；它会设置 should_exit，随后我们再关闭
    # stdio。纯 stdio 继续使用原有兜底 handler。
    if not unified:
        _register_signal_handlers()
    try:
        if unified:
            # app.main 的安全校验读取同一个 settings 对象。统一模式默认只绑
            # 回环地址，即使用户的 .env 没写 HOST，也不会触发 0.0.0.0 无鉴权拒绝。
            http_host = options.http_host or "127.0.0.1"
            http_port = options.http_port if options.http_port is not None else settings.port
            old_host, old_port = settings.host, settings.port
            settings.host, settings.port = http_host, http_port
            old_stdio_log_mode = os.environ.get("LUJO_MCP_STDIO_MODE")
            os.environ["LUJO_MCP_STDIO_MODE"] = "1"
            try:
                await _run_unified_transport(http_host, http_port)
            finally:
                if old_stdio_log_mode is None:
                    os.environ.pop("LUJO_MCP_STDIO_MODE", None)
                else:
                    os.environ["LUJO_MCP_STDIO_MODE"] = old_stdio_log_mode
                settings.host, settings.port = old_host, old_port
        else:
            await _run_stdio_transport()
    finally:
        # 正常 EOF / 协议退出路径
        cleanup_resources()


if __name__ == "__main__":
    asyncio.run(main())

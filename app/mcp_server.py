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
import socket
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
from app.mcp.protocol.server import (
    _acquire_slot_or_fastfail,
    _get_tool_executor_and_slots,
    _heavy_pool,
    _light_pool,
    _pool_for,
    _tool_registry,
    _validate_tool_arguments,
    get_agent_visible_tools,
    is_heavy_tool,
    tool_failure_predicate,
)
from app.mcp.protocol import shutdown as shutdown_mod
from app.mcp.protocol import server as protocol_server
from app.mcp.protocol.heavy_process import (
    close_all_jobs,
    run_heavy_tool_blocking,
    terminate_active_processes,
)
from app.mcp.protocol.tool_errors import ToolExecutionError
from app.mcp.tools import register_all_tools
from app.observability import (
    record_mcp_tool_busy,
    record_mcp_tool_call,
    record_mcp_tool_wait,
)
from app.observability import shutdown_observability

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
# B23（DESIGN_C1_SLOT_ACCOUNTING.md §6 表 #6）：幂等键改绑「executor 实例 +
# 槽位池代际」。原自持布尔 `_cleanup_done` 无任何调用路径能翻转——getter
# （_get_tool_executor）自愈重建出新实例后，第二次 cleanup 永久短路，第二代
# 资源泄漏。现在：同实例同代重复调用短路；实例被自愈重建或任一槽位池代际
# 推进后重新执行清理（两代资源都释放）。保存**实例引用**而非裸 id() 比较，
# 避免 CPython id 复用造成的假幂等。
_cleaned_executor: ThreadPoolExecutor | None = None
_cleaned_pool_generations: tuple[int, int] | None = None
_periodic_cleanup_task: asyncio.Task | None = None
_cleanup_lock = threading.Lock()


def cleanup_resources() -> None:
    """stdio 退出路径统一资源回收。

    幂等：幂等键为「当前 executor 实例 + 槽位池代际 id」——同实例同代多次
    调用（finally / atexit / signal）只执行一次；getter 自愈重建出新实例
    （或代际重建）后再次调用会清理**新**资源（B23：两代都释放）。
    回收顺序（C4 §3 / W4-4：M1 ①–⑥ 序，进程终止先于池关闭）：
      M1 ① 停止接纳（双池 begin_close：closing 置位，新 spawn/submit fastfail）
      M1 ② 取消旧 semaphore 等待者（禁止迁移）
      M1 ③ terminate_active_processes（10s 并行硬上限，进程终止）
      M1 ④ 关 Job 句柄（close_once，整树兜底）
      M1 ⑤ 最后才关池（两个池分别 shutdown(wait=False, cancel_futures=True)）
      其余既有职责（periodic/PG/excepthook/OTel）保留在 ⑤ 之后
      B23 幂等键（executor 实例 + 代际 id）保持在函数最前
    """
    global _cleaned_executor, _cleaned_pool_generations, _periodic_cleanup_task
    with _cleanup_lock:
        executor = _TOOL_EXECUTOR
        generations = (_light_pool.generation, _heavy_pool.generation)
        if _cleaned_executor is executor and _cleaned_pool_generations == generations:
            return
        _cleaned_executor = executor
        _cleaned_pool_generations = generations

        # 1) 取消后台 periodic_cleanup（防御性：当前 stdio 未启动该任务）
        task = _periodic_cleanup_task
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception as e:
                logger.warning(f"stdio 退出取消 periodic_cleanup 失败: {e}")

    # ── M1 ①–⑥（C4 §3；W4-4）：进程终止永远先于池关闭 ──
    def _m1_1():
        _light_pool.begin_close()
        _heavy_pool.begin_close()

    def _m1_2():
        _light_pool.cancel_waiters()
        _heavy_pool.cancel_waiters()

    def _m1_4():
        close_all_jobs()

    def _m1_5():
        # 两个池**分别** shutdown，仍不统一双池（硬禁区）；wait=False 不等
        # 运行中任务——被阻塞线程交看门狗收口（§2.2）
        for executor in (_TOOL_EXECUTOR, protocol_server._LIGHT_TOOL_EXECUTOR):
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.warning(f"stdio 退出关闭线程池失败: {e}")

    try:
        shutdown_mod.run_m1_sequence(
            t0=time.monotonic(),
            hooks={
                "step1_stop_accepting": _m1_1,
                "step2_cancel_waiters": _m1_2,
                "step3_terminate_active": terminate_active_processes,
                "step4_close_jobs": _m1_4,
                "step5_shutdown_pools": _m1_5,
                "step6_b23_idempotent": lambda: True,  # 幂等键已在函数最前生效
            },
            over_budget_events=lambda ev: logger.warning(
                "M1 步骤超预算: %s", ev
            ),
        )
    except Exception as e:
        logger.warning(f"stdio 退出 M1 序执行失败: {e}")

    # ── 其余既有职责（保留在 ⑤ 之后）──

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

    # 4) FIX(R8): 关闭 OTel 指标导出器。工具埋点 record_mcp_tool_* 会惰性
    # 创建 PeriodicExportingMetricReader 后台线程；纯 stdio 模式没有 FastAPI
    # lifespan（HTTP 侧由它负责关闭），此前该线程在退出时才被解释器收尾，
    # 边拆日志系统边向不可达端点重试，刷 "--- Logging error ---" 并拖慢退出。
    try:
        shutdown_observability()
    except Exception as e:
        logger.warning(f"stdio 退出关闭指标导出器失败: {e}")

    # （池关闭已由 M1 ⑤ 承接：两个池分别 shutdown(wait=False, cancel_futures=True)，
    # 且进程终止（M1 ③）永远先于池关闭——B15 现状错误序已修正）


def _signal_handler(signum, frame):
    """SIGINT/SIGTERM（C4 §1.1 / W4-3）：回调只发布退出意图与首次触发时间。

    清理**不在回调内**执行（旧实现在信号上下文运行清理，阻塞即挂死）——
    SystemExit 交给主循环 unwind，finally 中的清理在可调度上下文执行（幂等）。
    """
    shutdown_mod.signal_handler_stub(signum, frame)
    raise SystemExit(0)


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
    try:
        from app.rag.knowledge_base import bootstrap_knowledge_base

        bootstrap_knowledge_base()
    except Exception:
        logger.warning("知识库启动初始化失败，跳过（不影响启动）", exc_info=True)

    # C4 §1.1：EOF 感知接在唯一 stdin 输入生产者上——包装 sys.stdin.buffer
    # （不新增第二个读取线程、不缓冲），观察到 EOF 即记录退出意图与 t0。
    sys.stdin = shutdown_mod.wrap_stdin_with_eof_awareness(
        sys.stdin, lambda: shutdown_mod.record_exit_intent("stdio_eof")
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _http_port_conflict(host: str, port: int) -> str | None:
    """探测 HTTP 端口是否已被另一实例占用，返回冲突描述或 None。

    为什么要在 uvicorn 绑定之前自己查一次：不带本守卫时端口冲突也会失败，但
    失败发生在 ``from app.main import app`` 之后——那时 FastAPI 应用已导入、
    repair queue 已启动、知识库种子已加载，随后才由 uvicorn 抛出一行
    ``[Errno 10048] error while attempting to bind ...`` 再走完整优雅关闭。
    对宿主而言表现为「MCP 服务起来又静默退出，原因藏在 stderr 末尾」。
    提前探测可以：无副作用地立刻失败、给出端口号和两条可执行处置。

    探测刻意用不带 SO_REUSEADDR 的 socket（否则 Windows 上语义更松），探测完
    立即关闭，随后仍由 uvicorn 带 SO_REUSEADDR 正式绑定，保留其对 TIME_WAIT
    的容忍——不在正常重启路径上引入新的误报。
    """
    family = socket.AF_INET6 if (":" in host and not host.startswith(("0.0.0.0", "127."))) else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        return f"{host}:{port} ({exc.strerror or exc})"
    finally:
        probe.close()
    return None


async def _run_unified_transport(host: str, port: int) -> None:
    """在一个进程内并行运行 stdio MCP 与 FastAPI HTTP。

    两个 transport 共享同一套工具注册表和内存存储，因此浏览器 SDK 通过
    ``/ingest`` 写入的数据可以立即被 stdio MCP 客户端读取。任一 transport
    结束都会有序停止另一个，避免 stdio EOF 后留下孤儿 HTTP 进程。
    """
    conflict = _http_port_conflict(host, port)
    if conflict:
        # 显式失败而不是带着冲突继续。占用该端口的另一实例会收到浏览器 SDK 发往
        # 该地址的全部 /ingest 上报，本实例的存储则一条都收不到——宿主在当前会话
        # 里查到的永远是空现场，而这正是最难排查的一类问题。
        # SystemExit 的文案走 stderr，不污染 stdio 协议流。
        raise SystemExit(
            f"[lujo-mcp] HTTP 端口被占用: {conflict}\n"
            "该端口上已有另一个 Lujo 实例在提供采集服务，浏览器 SDK 的上报会全部"
            "进入它的存储，本实例查不到任何运行现场。\n"
            "处置：关闭另一个 Lujo 实例；或用 --http-port <n> 换一个端口"
            "（同时把 SDK 的 endpoint 指过去）；只需 stdio 工具时加 --no-http。"
        )

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

    shutdown_mod.wrap_uvicorn_handle_exit(http_server)  # C4 §1.1：serve 路径信号适配
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


async def _run_registered_tool(name: str, tool: dict, arguments: dict, *, token=None, pool=None):
    """执行工具。``token``/``pool`` 给定时，把**结算**挂到真实任务上（DESIGN C1 §3.1/§3.2）。

    槽位归还跟随「真实任务终结」，不再由调用方的 finally 无条件释放（B07 根因）。
    """
    timeout = settings.tool_timeout_seconds
    handler = tool["handler"]

    def _bind(done_obj) -> None:
        if token is not None and pool is not None:
            pool.attach(done_obj, token)

    if asyncio.iscoroutinefunction(handler) and not is_heavy_tool(name):
        # async **轻量**（repair_async 等）留进程内：真实任务即 Task，在 task
        # 上挂结算。heavy 判定先于 async 判定——async heavy 落入下方 heavy
        # 分支进子进程（C2 §6.1 B08，与 HTTP 侧同构）。
        task = asyncio.ensure_future(handler(arguments))
        if token is not None and pool is not None:
            task.add_done_callback(lambda _t: pool.settle(token))
        return await asyncio.wait_for(task, timeout=timeout)

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
        # W1 阶段：结算挂在「收割线程返回」上；W2 将改为由条目 REAPED 驱动。
        fut = loop.run_in_executor(
            None,
            run_heavy_tool_blocking,
            handler.__module__,
            handler.__name__,
            arguments,
            float(timeout),
        )
        if token is not None and pool is not None:
            fut.add_done_callback(lambda _f: pool.settle(token))
        return await fut

    # FIX P3-12: 轻量同步 handler 走专用有界线程池 _TOOL_EXECUTOR，不占默认池；
    # 超时只取消 await，线程继续运行但池有界不增长。
    # 关键：用 executor.submit 拿**真实** concurrent.futures.Future，回调挂它上面，
    # 线程真正结束时才结算并归还许可。
    real_future = _get_tool_executor().submit(handler, arguments)
    _bind(real_future)
    return await asyncio.wait_for(asyncio.wrap_future(real_future), timeout=timeout)


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

    FIX(R8): 入参校验与轻/重双池门控改与 HTTP 侧共用同一实现
    （``app.mcp.protocol.server``），此前 stdio 两者皆无：

    1. 官方 SDK 只对出现在 tools/list 里的工具做 jsonschema 校验，而
       ``agent_visible=False`` 的 SDK 上报类工具刻意不进清单 → stdio 上
       完全绕过校验（HTTP 侧则一律先校验）。缺参/类型错入参会直接落到
       handler 变成 TOOL_INTERNAL，宿主拿不到可自纠错的参数级错误。
    2. stdio 不做槽位门控 → 重型工具（verify_ui 等）可被无上限并发调用，
       每次都拉起一个浏览器子进程；HTTP 侧同样入参只允许
       ``tool_heavy_executor_workers`` 个并发。stdio 是 npm 默认传输，
       这条路径的资源上限此前形同不存在。
    3. stdio 不记 MCP 工具指标 → mcp_tool_* 指标只反映 HTTP 流量。
    """
    _tool_start = time.monotonic()
    tool = _tool_registry.get(name)
    if tool is None:
        record_mcp_tool_call(name, "error", 0.0)
        raise ToolExecutionError(json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False))

    validation_error = _validate_tool_arguments(tool, arguments)
    if validation_error:
        record_mcp_tool_call(name, "invalid_params", 0.0)
        raise ToolExecutionError(json.dumps(
            {"error": validation_error, "error_code": "INVALID_PARAMS"},
            ensure_ascii=False,
        ))

    # 与 HTTP 侧同一套轻/重分池信号量：async 工具不需要线程池，仅取槽位门控。
    # B20：槽位信号量经代际属主 getter 获取（_get_tool_executor_and_slots 内部
    # 每次重读当前代）；等待者登记进同一属主，代际关闭时统一取消（TOOL_BUSY）。
    _, slots, pool_type = _get_tool_executor_and_slots(name)
    busy_timeout = settings.tool_busy_queue_timeout
    wait_start = time.perf_counter()
    if not await _acquire_slot_or_fastfail(slots, busy_timeout, pool=_pool_for(pool_type)):
        wait_sec = time.perf_counter() - wait_start
        record_mcp_tool_busy(name, pool_type, wait_sec)
        record_mcp_tool_call(name, "busy", wait_sec)
        logger.warning("工具 %s (%s池) 执行队列已满，已拒绝执行", name, pool_type)
        raise ToolExecutionError(json.dumps(
            {"error": "工具执行队列已满，请稍后重试。", "error_code": "TOOL_BUSY", "_busy": True},
            ensure_ascii=False,
        ))
    record_mcp_tool_wait(name, pool_type, time.perf_counter() - wait_start)

    # DESIGN C1 §3.1/§3.4：登记 ACTIVE token，把**结算**挂到真实任务上；
    # 删除运行期无条件 slots.release()（B07「超发」根因）。
    _pool = _pool_for(pool_type)
    _token = _pool.acquire_token(loop=asyncio.get_running_loop(), semaphore=slots)
    try:
        result = await _run_registered_tool(name, tool, arguments, token=_token, pool=_pool)
        record_mcp_tool_call(name, "ok", time.monotonic() - _tool_start)
    except asyncio.TimeoutError:
        # 不结算：真实任务可能仍在运行，槽位由真实 future/task 的回调在终结时归还
        record_mcp_tool_call(name, "timeout", settings.tool_timeout_seconds)
        logger.warning("工具 %s 执行超时（>%ss），已中止", name, settings.tool_timeout_seconds)
        raise ToolExecutionError(json.dumps(
            {"error": f"工具执行超时（>{settings.tool_timeout_seconds}s），已中止。", "_timed_out": True},
            ensure_ascii=False,
        ))
    except ToolExecutionError:
        # 工具已实际执行并产出失败结果：结算已由回调完成，此处不重复
        raise
    except asyncio.CancelledError:
        # 取消传播：真实任务可能仍在运行，交由回调结算（不在此处归还）
        raise
    except Exception as e:
        record_mcp_tool_call(name, "error", time.monotonic() - _tool_start)
        logger.error(str(e), exc_info=True)
        # submit 抛错（池已关闭）等「任务从未入队」路径需补偿结算；
        # handler 自身抛错时真实 future/task 已完成、回调已结算，此处幂等空转。
        _pool.settle(_token)
        raise ToolExecutionError(json.dumps({"error": "Tool execution failed"}, ensure_ascii=False))

    if tool_failure_predicate(tool)(result):
        raise ToolExecutionError(json.dumps(result, ensure_ascii=False, indent=2))

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
    # C4 §1.1：独立入口（纯 stdio / 统一模式）在接纳调用前创建并确认就绪
    # 进程级监督线程；创建失败即启动失败（嵌入式宿主不经过本入口，只有
    # 代际关闭权限）。
    _exit_supervisor = shutdown_mod.ensure_exit_supervisor()
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
        # ── B05: 统一知识库启动初始化（回灌 + 种子加载，stdio/HTTP 共享）──
        try:
            from app.rag.knowledge_base import bootstrap_knowledge_base

            bootstrap_knowledge_base()
        except Exception:
            logger.warning("知识库启动初始化失败，跳过（不影响启动）", exc_info=True)

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

"""
全局异常自动捕获钩子。

不装这个的话，AI 只能通过手动调用 capture_exception 才能记录到错误——
实际使用中你更需要的是"项目里随便哪里报错了，都自动被记下来，
AI 随时能通过 list_recent_traces 看到最新情况"。

覆盖两类场景：
1. 同步代码里没被 try/except 捕获、一路抛到顶层的异常 -> sys.excepthook
2. asyncio 任务里没被 await 捕获的异常 -> asyncio 的 exception handler
FastAPI 请求处理中的异常单独在 api 层用 middleware 捕获（因为 Starlette
会在中间件链路里吞掉一部分异常信息，不会都跑到 sys.excepthook）。
"""
import asyncio
import logging
import sys
from types import TracebackType

from app.runtime.collectors.stacktrace import capture_exception
from app.runtime.core.errors import record as record_error
from app.runtime.core.redaction import redact

# FIX: P1-D2 —— 此前单一 _installed 标志：首次在无事件循环的上下文安装
# （如模块导入期/stdio 启动，asyncio 部分被跳过但标志已置位），之后在
# lifespan（有 loop）里按 docstring 建议"再调用一次"会因幂等检查直接
# return——asyncio 任务异常捕获永久失效。现拆分为两个独立标志，支持
# "先装 sys.excepthook、事件循环就绪后补装 asyncio handler"的两段式安装。
_excepthook_installed = False
_asyncio_installed = False
_original_hook = None  # install 时保存，供 uninstall 恢复
_original_asyncio_handler = None  # asyncio loop 的原 handler（可能为 None）
logger = logging.getLogger("lujo-mcp.exception-hook")


def _redact_exception_data(data: dict) -> dict:
    """对 capture_exception 返回的 message / traceback 字段做脱敏。"""
    if "message" in data:
        data["message"] = redact(data["message"])
    if "traceback" in data:
        data["traceback"] = redact(data["traceback"])
    return data


def install_global_hook():
    """在应用启动时调用一次即可，幂等。

    FIX: P1-D2 —— 两段式安装：sys.excepthook 与 asyncio loop handler 各自
    独立安装。首次在无事件循环上下文调用时只装 excepthook；事件循环就绪后
    再次调用会**补装** asyncio handler（不再因单一标志直接跳过——此前 asyncio
    任务异常捕获会永久失效）。两部分均已安装时为纯幂等 no-op。
    """
    global _excepthook_installed, _asyncio_installed, _original_hook, _original_asyncio_handler

    # ── 1. sys.excepthook（同步顶层异常）──
    if not _excepthook_installed:
        _original_hook = sys.excepthook

        def _hook(exc_type: type[BaseException], exc_value: BaseException, tb: TracebackType | None):
            try:
                exc_data = capture_exception(exc_value, source="global_hook")
                exc_data["message"] = redact(exc_data.get("message", ""))
                exc_data["traceback"] = redact(exc_data.get("traceback", ""))
                record_error(exc_data, source="global_hook")
            except Exception as e:
                logger.error(f"Exception hook failed: {e}", exc_info=True)
            _original_hook(exc_type, exc_value, tb)

        sys.excepthook = _hook
        _excepthook_installed = True

    # ── 2. asyncio loop exception handler（未 await 的任务异常）──
    if not _asyncio_installed:
        def _asyncio_handler(loop, context):
            exc = context.get("exception")
            if exc is not None:
                try:
                    exc_data = capture_exception(exc, source="asyncio_loop", extra={"message": context.get("message", "")})
                    exc_data["message"] = redact(exc_data.get("message", ""))
                    exc_data["traceback"] = redact(exc_data.get("traceback", ""))
                    record_error(exc_data, source="asyncio_loop")
                except Exception as e:
                    logger.error(f"Exception hook failed: {e}", exc_info=True)
            loop.default_exception_handler(context)

        try:
            loop = asyncio.get_running_loop()
            _original_asyncio_handler = loop.get_exception_handler()
            loop.set_exception_handler(_asyncio_handler)
            _asyncio_installed = True
        except RuntimeError:
            # 当前没有运行中的事件循环，跳过；FastAPI启动后会有自己的loop，
            # 在 lifespan/startup 事件里再调用一次 install_global_hook() 补装
            # （FIX: P1-D2 —— 补装现在真的会生效）
            pass


def uninstall_global_hook():
    """卸载全局异常钩子，恢复原 hook。幂等：未安装时直接返回。

    主要用于 stdio 子进程退出路径，避免测试间污染和资源泄漏。
    按两部分各自的实际安装状态独立恢复。
    """
    global _excepthook_installed, _asyncio_installed, _original_hook, _original_asyncio_handler

    if not (_excepthook_installed or _asyncio_installed):
        return

    # 恢复 sys.excepthook
    if _excepthook_installed:
        try:
            sys.excepthook = _original_hook if _original_hook is not None else sys.__excepthook__
        except Exception as e:
            logger.warning(f"恢复 sys.excepthook 失败: {e}")
        _excepthook_installed = False
        _original_hook = None

    # 恢复 asyncio loop exception handler（若已安装且 loop 仍可用）
    if _asyncio_installed:
        try:
            loop = asyncio.get_running_loop()
            if _original_asyncio_handler is None:
                loop.set_exception_handler(None)
            else:
                loop.set_exception_handler(_original_asyncio_handler)
        except RuntimeError:
            # loop 已关闭或不存在，无需恢复
            pass
        except Exception as e:
            logger.warning(f"恢复 asyncio loop handler 失败: {e}")
        _asyncio_installed = False
        _original_asyncio_handler = None

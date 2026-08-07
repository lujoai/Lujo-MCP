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
from app.mcp.core.errors import record as record_error
from app.runtime.core.redaction import redact

_installed = False
_original_hook = None  # install 时保存，供 uninstall 恢复
_original_asyncio_handler = None  # asyncio loop 的原 handler（可能为 None）
logger = logging.getLogger("ai-debug-mcp.exception-hook")


def _redact_exception_data(data: dict) -> dict:
    """对 capture_exception 返回的 message / traceback 字段做脱敏。"""
    if "message" in data:
        data["message"] = redact(data["message"])
    if "traceback" in data:
        data["traceback"] = redact(data["traceback"])
    return data


def install_global_hook():
    """在应用启动时调用一次即可，幂等。"""
    global _installed, _original_hook, _original_asyncio_handler
    if _installed:
        return

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
    except RuntimeError:
        # 当前没有运行中的事件循环，跳过；FastAPI启动后会有自己的loop，
        # 建议在 lifespan/startup 事件里再调用一次 install_global_hook()
        pass

    _installed = True


def uninstall_global_hook():
    """卸载全局异常钩子，恢复原 hook。幂等：未安装时直接返回。

    主要用于 stdio 子进程退出路径，避免测试间污染和资源泄漏。
    """
    global _installed, _original_hook, _original_asyncio_handler
    if not _installed:
        return

    # 恢复 sys.excepthook
    try:
        sys.excepthook = _original_hook if _original_hook is not None else sys.__excepthook__
    except Exception as e:
        logger.warning(f"恢复 sys.excepthook 失败: {e}")

    # 恢复 asyncio loop exception handler（若 loop 仍可用）
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

    _installed = False
    _original_hook = None
    _original_asyncio_handler = None
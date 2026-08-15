"""全局异常处理器 —— 兜底所有未捕获异常，防止服务崩溃"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("lujo-mcp.errors")


def setup_error_handlers(app: FastAPI):
    """注册全局异常处理器"""

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """兜底所有未预期的异常，返回 500 但不让进程崩溃"""
        logger.exception(
            f"全局异常捕获: {type(exc).__name__}",
            extra={
                "method": request.method,
                "path": str(request.url.path),
                "error": str(exc),
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"服务内部错误: {type(exc).__name__}",
                "trace_id": getattr(request.state, "trace_id", "unknown"),
            },
        )


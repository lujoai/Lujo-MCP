"""堆栈追踪收集器 —— 捕获异常的调用栈信息"""

import linecache
import sys
import traceback
from typing import Optional

from app.runtime.core.redaction import redact

SENSITIVE_KEYS = {
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "passwd",
    "pwd",
}


def capture_exception(
    exc: Optional[BaseException] = None,
    source: str = "manual",
    extra: Optional[dict] = None,
) -> dict:
    """捕获异常信息，返回结构化的错误描述"""
    if exc is None:
        exc = sys.exc_info()[1]
        if exc is None:
            return {"error": "当前上下文中没有异常"}

    tb = exc.__traceback__
    frames = []

    while tb is not None:
        frame = tb.tb_frame
        filename = frame.f_code.co_filename
        lineno = tb.tb_lineno
        func_name = frame.f_code.co_name

        # 获取源码行
        source_line = linecache.getline(filename, lineno).strip() if filename != "<string>" else "<dynamic>"

        # 提取局部变量（只保留简单类型，避免循环引用的 __repr__ 问题）
        local_vars = {}
        for key, val in frame.f_locals.items():
            try:
                if key.lower() in SENSITIVE_KEYS:
                    local_vars[key] = "***REDACTED***"
                else:
                    local_vars[key] = redact(repr(val))
            except Exception:
                local_vars[key] = "<unable to render>"

        frames.append({
            "file": filename,
            "line": lineno,
            "function": func_name,
            "code": source_line,
            "locals": local_vars,
        })
        tb = tb.tb_next

    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "source": source,
        "extra": extra or {},
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "frames": frames,
        "frame_count": len(frames),
    }


def format_trace_for_ai(exc_data: dict, max_locals: int = 5) -> str:
    """将异常数据格式化为适合 AI 分析的文本"""
    lines = [
        f"异常类型: {exc_data.get('type', 'Unknown')}",
        f"异常信息: {exc_data.get('message', '')}",
        f"调用栈深度: {exc_data.get('frame_count', 0)}",
        "",
        "调用栈:",
    ]

    frames = exc_data.get("frames", [])
    for i, frame in enumerate(reversed(frames)):
        lines.append(
            f"  #{i} {frame['file']}:{frame['line']} in {frame['function']}"
        )
        if frame["code"]:
            lines.append(f"      → {frame['code']}")
        # 只展示前几个局部变量
        locals_shown = list(frame["locals"].items())[:max_locals]
        if locals_shown:
            lines.append(f"      局部变量: {dict(locals_shown)}")

    return redact("\n".join(lines))

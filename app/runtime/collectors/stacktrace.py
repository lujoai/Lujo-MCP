"""调用栈追踪收集器 —— 捕获异常的调用栈信息与智能框架栈帧折叠。"""

import linecache
import os
import sys
import traceback
from typing import Optional

from app.runtime.core.redaction import redact, is_sensitive_key

# FIX: CR-2 —— 此前 SENSITIVE_KEYS 为 8 个键名的精确匹配集合，refresh_token /
# client_secret / session_token 等复合键在捕获 locals 时漏脱敏。
# 改为复用 redaction 的白名单感知子串判定（含 redaction_key_allowlist，
# password_hash / public_key 等白名单字段仍可在捕获期保留原值）。

# FIX: P2-D5 —— 单个局部变量 repr 的最大字符数。异常路径上一个超大局部变量
# （大 dict / DataFrame / 长字符串等）的 repr 可达数十 MB，直接进入内存缓冲 /
# PG / 响应进程 OOM。与 parse_network_record 等模块的 10KB 截断纪律保持一致。
_LOCALS_REPR_MAX_CHARS = 10000

_FRAMEWORK_PATH_PARTS = (
    "site-packages",
    "dist-packages",
    "node_modules",
    ".venv",
    "venv",
    "virtualenv",
    "lib/python",
    "Lib/site-packages",
    "lib\\python",
    "Lib\\site-packages",
)

_KNOWN_FRAMEWORKS = (
    "starlette",
    "fastapi",
    "uvicorn",
    "anyio",
    "asyncio",
    "pydantic",
    "httpx",
    "werkzeug",
    "flask",
    "django",
    "pytest",
    "_pytest",
    "pluggy",
    "urllib3",
    "requests",
    "sqlalchemy",
    "psycopg2",
    "asyncpg",
    "qdrant_client",
    "click",
    "typer",
)


def is_framework_frame(file_path: str) -> bool:
    """判定栈帧是否为 Python 标准库、三方库或框架内部代码。"""
    if not file_path:
        return True
    p = str(file_path).replace("\\", "/")
    # 动态代码 / 内部虚拟文件
    if p.startswith("<") and p.endswith(">"):
        return True
    # 三方库目录特征
    for part in _FRAMEWORK_PATH_PARTS:
        if part in p:
            return True
    # 标准库特征
    for std in getattr(sys, "path", []):
        if std:
            std_norm = str(std).replace("\\", "/")
            if p == std_norm or p.startswith(std_norm + "/"):
                # 如果 std 是当前工作区路径，则不视作 stdlib
                try:
                    if os.path.realpath(std) == os.path.realpath(os.getcwd()):
                        continue
                except Exception:
                    pass
                return True
    return False


def _detect_framework_name(file_path: str) -> str:
    """提取栈帧所属框架或库名称（用于可读性汇总）。"""
    p = str(file_path or "").replace("\\", "/").lower()
    for fw in _KNOWN_FRAMEWORKS:
        if f"/{fw}/" in p or f"\\{fw}\\" in p or f"/{fw}." in p:
            return fw
    if "asyncio" in p:
        return "asyncio"
    if "<frozen" in p or "<built-in" in p:
        return "internal"
    if "site-packages" in p or "dist-packages" in p:
        return "third-party"
    if "lib/python" in p or "lib\\python" in p:
        return "stdlib"
    return "framework"


def fold_stack_frames(frames: list[dict], min_fold_count: int = 2) -> list[dict]:
    """智能折叠连续的框架与三方库栈帧。

    - 连续 >= min_fold_count 个框架帧会被折叠为一个汇总帧；
    - 业务项目代码帧永远完整保留；
    - 如果整个异常抛出点（最内层帧）在框架内，抛出点帧会单独保留，确保根因线索不丢失。
    """
    if not frames:
        return []

    result: list[dict] = []
    current_fw_group: list[dict] = []

    def flush_fw_group():
        if not current_fw_group:
            return
        if len(current_fw_group) < min_fold_count:
            result.extend(current_fw_group)
        else:
            fws = []
            for f in current_fw_group:
                name = _detect_framework_name(f.get("file", ""))
                if name and name not in fws:
                    fws.append(name)
            fw_summary = " -> ".join(fws) if fws else "internal"
            count = len(current_fw_group)
            first_f = current_fw_group[0]
            last_f = current_fw_group[-1]
            result.append({
                "file": "<framework-frames-folded>",
                "line": 0,
                "function": f"[... {count} framework frames folded: {fw_summary} ...]",
                "code": "",
                "locals": {},
                "is_folded": True,
                "folded_count": count,
                "frameworks": fws,
                "first_frame": f"{first_f.get('file')}:{first_f.get('line')}",
                "last_frame": f"{last_f.get('file')}:{last_f.get('line')}",
            })
        current_fw_group.clear()

    for idx, frame in enumerate(frames):
        is_last_frame = (idx == len(frames) - 1)
        # 最内层抛出点如果是最后一个帧，不折叠到前面的组里，保证根因直接可见
        if is_last_frame and current_fw_group and is_framework_frame(frame.get("file", "")):
            flush_fw_group()
            result.append(frame)
            continue

        if is_framework_frame(frame.get("file", "")):
            current_fw_group.append(frame)
        else:
            flush_fw_group()
            result.append(frame)

    flush_fw_group()
    return result


def _truncate_locals_repr(val) -> str:
    """对局部变量做 repr 并截断到上限，返回安全的字符串。

    P2-D5：repr(repr) 对大对象不产生长度上限，可能膨胀到数十 MB。仅在长度
    超限时才追加截断标记（正常小对象与旧行为完全一致，零开销）。
    """
    s = repr(val)
    if len(s) > _LOCALS_REPR_MAX_CHARS:
        s = s[:_LOCALS_REPR_MAX_CHARS] + f"...<truncated {len(s) - _LOCALS_REPR_MAX_CHARS} chars>"
    return s


def capture_exception(
    exc: Optional[BaseException] = None,
    source: str = "manual",
    extra: Optional[dict] = None,
) -> dict:
    """捕获异常信息，返回结构化的错误数据"""
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

        # 获取源代码行
        source_line = linecache.getline(filename, lineno).strip() if filename != "<string>" else "<dynamic>"

        # 获取局部变量（只转字符串，避免循环引用的 __repr__ 问题）
        local_vars = {}
        for key, val in frame.f_locals.items():
            try:
                if is_sensitive_key(key):
                    local_vars[key] = "***REDACTED***"
                else:
                    local_vars[key] = redact(_truncate_locals_repr(val))
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


def format_trace_for_ai(
    exc_data: dict,
    max_locals: int = 5,
    fold_frameworks: bool = True,
) -> str:
    """将异常数据格式化为适合 AI 阅读的文本，支持框架栈帧智能折叠。"""
    raw_frames = exc_data.get("frames", [])
    effective_frames = fold_stack_frames(raw_frames) if fold_frameworks else raw_frames

    lines = [
        f"异常类型: {exc_data.get('type', 'Unknown')}",
        f"异常信息: {exc_data.get('message', '')}",
        f"调用栈深度: {exc_data.get('frame_count', len(raw_frames))}",
        "",
        "调用栈 (从外层到抛出点):",
    ]

    for i, frame in enumerate(effective_frames):
        if frame.get("is_folded"):
            lines.append(f"  #{i} {frame['function']}")
            continue

        is_proj = not is_framework_frame(frame.get("file", ""))
        tag = " [PROJECT CODE]" if is_proj else ""
        lines.append(
            f"  #{i} {frame.get('file', '')}:{frame.get('line', 0)} in {frame.get('function', '')}{tag}"
        )
        if frame.get("code"):
            lines.append(f"      行 {frame['code']}")
        locals_shown = list(frame.get("locals", {}).items())[:max_locals]
        if locals_shown:
            lines.append(f"      局部变量: {dict(locals_shown)}")

    return redact("\n".join(lines))

"""
代码定位器 —— 这是整个项目里对"节省排查时间"贡献最大的一个模块。

给定堆栈帧的 file + line，自动读取该行附近的源码，带高亮标记，
并生成可点击的 IDE 链接（vscode://file/...:行号），让宿主 AI / 开发者
不需要再单独打开文件、翻到对应行号，一次调用就拿到
"哪里错了 + 错误代码长什么样 + 一点即达"。
"""

import os
import linecache
from typing import Optional

from app.config import settings
from app.schemas.context import CodeSnippet


def _remap_path(file_path: str) -> str:
    """按 SOURCE_PATH_MAP 把远程/容器路径映射为本地路径。"""
    raw = (settings.source_path_map or "").strip()
    if not raw:
        return file_path
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        remote, local = pair.split(":", 1)
        remote, local = remote.strip(), local.strip()
        if remote and file_path.startswith(remote):
            return local + file_path[len(remote):]
    return file_path


def _allowed_roots() -> list[str]:
    """允许读取的根目录列表。
    配置了 whitelist_path_prefix 则用配置；否则（SEC-01）默认收敛到进程工作目录
    （stdio 模式下即被调试项目根），默认拒绝根目录之外的任意路径。"""
    prefix = (settings.whitelist_path_prefix or "").strip()
    if prefix:
        return [os.path.abspath(p.strip()) for p in prefix.split(",") if p.strip()]
    return [os.path.abspath(os.getcwd())]


def _is_allowed(path: str) -> bool:
    """路径白名单校验（SEC-01：默认拒绝允许根之外的任意路径，防任意文件读取 / LFI）。"""
    abs_path = os.path.abspath(path)
    for root in _allowed_roots():
        # 用 os.sep 边界比较，避免 /app 命中 /app-secrets
        if abs_path == root or abs_path.startswith(root + os.sep):
            return True
    return False


def make_ide_link(file_path: str, line_no: int) -> Optional[str]:
    """生成可点击的 IDE 链接；路径不在白名单时返回 None。"""
    abs_path = os.path.abspath(_remap_path(file_path))
    if not _is_allowed(abs_path):
        return None
    scheme = (settings.ide_scheme or "vscode").lower()
    if scheme == "vscode":
        return f"vscode://file/{abs_path}:{line_no}"
    return f"file://{abs_path}"


def get_code_snippet(file_path: str, line_no: int, context_lines: int | None = None) -> CodeSnippet:
    context_lines = context_lines or settings.code_context_lines

    # 路径白名单校验，禁止任意路径读取
    abs_path = os.path.abspath(_remap_path(file_path))
    if not _is_allowed(abs_path):
        return CodeSnippet(
            file=file_path,
            error_line=line_no,
            snippet="",
            found=False,
            link=None,
        )

    # linecache 对不存在的文件/行会返回空字符串，不会抛异常
    linecache.checkcache(file_path)
    first_line = linecache.getline(file_path, 1)
    if not first_line and line_no > 0:
        # 尝试确认文件是否真的读取不到（比如属于第三方库的 .pyc 或路径已变化）
        probe = linecache.getline(file_path, line_no)
        if not probe:
            return CodeSnippet(
                file=file_path,
                error_line=line_no,
                snippet="",
                found=False,
                link=make_ide_link(file_path, line_no),
            )

    start = max(1, line_no - context_lines)
    end = line_no + context_lines
    lines = []
    for i in range(start, end + 1):
        text = linecache.getline(file_path, i)
        if not text:
            continue
        marker = ">>> " if i == line_no else "    "
        lines.append(f"{marker}{i}: {text.rstrip()}")

    snippet = "\n".join(lines)
    return CodeSnippet(
        file=file_path,
        error_line=line_no,
        snippet=snippet if snippet else "(无法读取该文件源码，可能是内置模块或路径不存在)",
        found=bool(snippet),
        link=make_ide_link(file_path, line_no),
    )


def get_snippets_for_frames(frames: list[dict], context_lines: int | None = None) -> list[CodeSnippet]:
    """批量处理堆栈里的每一帧,frames 结构对应 StackFrame.model_dump()"""
    return [get_code_snippet(f["file"], f["line"], context_lines) for f in frames]

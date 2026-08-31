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


def _split_remote_local(pair: str) -> tuple[str, str]:
    """按 remote:local 拆分 SOURCE_PATH_MAP 的单个映射对，兼容 Windows 盘符。

    FIX(v0.7.1-b8-5): 此前 ``pair.split(":", 1)`` 在 Windows 上把盘符冒号当分隔符
    （"C:\\app:C:\\local" 被拆成 remote="C"、local="\\app:C:\\local"），映射失效。
    规则：若对以「字母+冒号」（Windows 盘符）开头，则跳过首个冒号、取第二个冒号作
    分隔符；否则取首个冒号。返回 (remote, local)，均已 strip。
    """
    pair = pair.strip()
    if len(pair) >= 2 and pair[1] == ":" and pair[0].isalpha():
        rest = pair[2:]
        idx = rest.find(":")
        if idx >= 0:
            return pair[: idx + 2].strip(), rest[idx + 1 :].strip()
    if ":" in pair:
        remote, local = pair.split(":", 1)
        return remote.strip(), local.strip()
    return "", ""


def _remap_path(file_path: str) -> str:
    """按 SOURCE_PATH_MAP 把远程/容器路径映射为本地路径。"""
    raw = (settings.source_path_map or "").strip()
    if not raw:
        return file_path
    for pair in raw.split(","):
        remote, local = _split_remote_local(pair)
        if remote and file_path.startswith(remote):
            return local + file_path[len(remote):]
    return file_path


def _allowed_roots() -> list[str]:
    """允许读取的根目录列表。
    配置了 whitelist_path_prefix 则用配置；否则（SEC-01）默认收敛到进程工作目录
    （stdio 模式下即被调试项目根），默认拒绝根目录之外的任意路径。"""
    prefix = (settings.whitelist_path_prefix or "").strip()
    if prefix:
        return [os.path.realpath(p.strip()) for p in prefix.split(",") if p.strip()]
    return [os.path.realpath(os.getcwd())]


def _is_allowed(path: str) -> bool:
    """路径白名单校验（SEC-01：默认拒绝允许根之外的任意路径，防任意文件读取 / LFI）。

    realpath 解析符号链接：白名单根内的 symlink 指向根外文件时不得绕过校验
    （与 static_analyzer 的实现保持一致）。"""
    abs_path = os.path.realpath(path)
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
        # FIX(v0.7.1-b8-1): Windows 路径反斜杠在 URL 中非法（\U/\A 等被当转义序列，
        # vscode:// 客户端无法解析）；统一转正斜杠（vscode 协议兼容正斜杠路径）。
        return f"vscode://file/{abs_path.replace('\\', '/')}:{line_no}"
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
    # 注意：必须用 abs_path（已按 SOURCE_PATH_MAP 映射 + abspath 后的本地路径）读取，
    # 否则白名单校验通过但 linecache 仍按原始远程路径读取 → 永远读不到本地文件。
    linecache.checkcache(abs_path)
    first_line = linecache.getline(abs_path, 1)
    if not first_line and line_no > 0:
        # 尝试确认文件是否真的读取不到（比如属于第三方库的 .pyc 或路径已变化）
        probe = linecache.getline(abs_path, line_no)
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
        text = linecache.getline(abs_path, i)
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
    """批量获取调用栈中每一帧的源码片段，自动过滤折叠帧与虚拟文件。

    FIX(v0.7.1-b2-6): 畸形帧（line 缺失/None/非可转整数）逐帧跳过——
    此前 f["line"] 直索引 + 后续算术，单帧缺 line 抛 KeyError、line=None
    抛 TypeError，被 builder 兜底吞掉后整批 code_snippets 全部丢失。
    单帧读取异常也只丢该帧，不再污染整批。
    """
    valid_frames = [
        f for f in frames
        if f and not f.get("is_folded") and f.get("file") and not str(f.get("file")).startswith("<")
    ]
    snippets: list[CodeSnippet] = []
    for f in valid_frames:
        raw_line = f.get("line")
        if raw_line is None or isinstance(raw_line, bool):
            continue
        try:
            line_no = int(raw_line)
        except (TypeError, ValueError):
            continue
        try:
            snippets.append(get_code_snippet(f["file"], line_no, context_lines))
        except Exception:
            continue
    return snippets

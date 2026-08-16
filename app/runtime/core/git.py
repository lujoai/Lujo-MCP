"""
Git 信息集成 —— 为堆栈帧提供 blame 与最近 diff，帮助 AI 判断错误是否近期改动引入。

安全设计（proj1 增强，proj2 缺失）：
- 所有 git 命令带超时（settings.git_timeout），超时/失败返回 None，不阻断主流程。
- 路径白名单（settings.git_path_whitelist）：非空时仅允许白名单前缀下的文件，
  防止通过任意路径探测其他 git 仓库内容（信息泄露）。
- commits_back 限制在 1..50，防滥用。
按 proj1 架构重写（非复制 proj2）。
"""
import os
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("lujo-mcp.git")

_MAX_COMMITS_BACK = 50


def _is_allowed(file_path: str) -> bool:
    """路径白名单校验（SEC-01）。
    配置了 git_path_whitelist 用配置；否则默认收敛到进程工作目录，默认拒绝目录外路径，
    防止通过任意路径探测其他 git 仓库历史（信息泄露）。"""
    prefix = (settings.git_path_whitelist or "").strip()
    if prefix:
        allowed = [os.path.realpath(p.strip()) for p in prefix.split(",") if p.strip()]
    else:
        allowed = [os.path.realpath(os.getcwd())]
    # realpath 解析符号链接，防止白名单根内 symlink 指向根外路径绕过校验
    abs_path = os.path.realpath(file_path)
    # 用 os.sep 边界比较，避免 /app 命中 /app-secrets
    return any(abs_path == p or abs_path.startswith(p + os.sep) for p in allowed)


def _git_cmd(args: list[str], cwd: Path) -> str | None:
    """执行 git 命令，带超时；失败返回 None。"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            # git 输出为 UTF-8；Windows 上 text=True 默认按本地 gbk 解码会 UnicodeDecodeError，
            # 导致 diff/blame 静默失败。显式 utf-8 + errors=replace 兜底非法字节。
            encoding="utf-8",
            errors="replace",
            timeout=settings.git_timeout,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("git 命令超时: %s (cwd=%s)", args[0], cwd)
        return None
    except Exception:
        return None


def _parse_blame_line(porcelain: str) -> Optional[dict]:
    """解析 `git blame -L n,n --porcelain` 单行输出。"""
    lines = porcelain.splitlines()
    if not lines:
        return None

    commit = lines[0].split()[0] if lines[0].split() else ""
    author = ""
    author_time = ""
    summary = ""
    line_text = ""

    for line in lines:
        if line.startswith("author "):
            author = line[7:]
        elif line.startswith("author-time "):
            try:
                ts = int(line[12:])
                author_time = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except ValueError:
                author_time = line[12:]
        elif line.startswith("summary "):
            summary = line[8:]
        elif line.startswith("\t"):
            line_text = line[1:]

    if not commit or commit.startswith("0000000"):
        return None  # 未跟踪行

    return {
        "commit": commit,
        "author": author,
        "date": author_time,
        "summary": summary,
        "line_text": line_text,
    }


def get_blame_for_frame(file_path: str, line_no: int) -> Optional[dict]:
    """返回指定文件/行最后是谁在哪次 commit 改的。"""
    if not _is_allowed(file_path):
        logger.warning("git blame 被白名单拒绝: %s", file_path)
        return None

    line_no = int(line_no)

    path = Path(file_path)
    if not path.exists():
        return None

    out = _git_cmd(["blame", "-L", f"{line_no},{line_no}", "--porcelain", str(path)], path.parent)
    if not out:
        return None

    parsed = _parse_blame_line(out)
    if not parsed:
        return None

    return {"file": file_path, "line": line_no, **parsed}


def get_recent_diff(file_path: str, commits_back: int = 3) -> Optional[dict]:
    """返回指定文件最近 N 次 commit 的 diff。"""
    if not _is_allowed(file_path):
        logger.warning("git diff 被白名单拒绝: %s", file_path)
        return None

    commits_back = max(1, min(int(commits_back), _MAX_COMMITS_BACK))

    path = Path(file_path)
    if not path.exists():
        return None

    out = _git_cmd(["diff", f"HEAD~{commits_back}", "--", str(path)], path.parent)
    if not out:
        return None

    return {"file": file_path, "commits_back": commits_back, "diff": out}

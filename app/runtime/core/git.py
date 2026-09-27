"""
Git 信息集成 —— 为堆栈帧提供 blame 与最近 diff，帮助 AI 判断错误是否近期改动引入。

安全设计（proj1 增强，proj2 缺失）：
- 所有 git 命令带超时（settings.git_timeout），超时/失败返回 None，不阻断主流程。
- 授权项目根（settings.git_path_whitelist）：P1-D 起语义为「授权项目根目录」——
  逗号分隔绝对路径，查询路径先规范化（resolve 解析 ../ 与符号链接），再按
  os.path.normcase 归一（Windows 大小写不敏感）判定是否位于某个授权根之下；
  **为空时收敛到进程工作目录**（不是「不限制」——W9 / P3-SEC-1 已更正），
  两种情况都默认拒绝授权根之外的路径，防止通过任意路径探测其他 git
  仓库内容（信息泄露）。拒绝时经 check_path_allowed 返回结构化原因，不再静默。
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


def get_authorized_roots() -> list[str]:
    """当前生效的 git 授权项目根列表（已 realpath 规范化，保留原始大小写供展示）。

    GIT_PATH_WHITELIST 配置了非空条目时即授权根列表；为空（或只剩空白/逗号）
    时默认收敛为本服务进程工作目录——安全默认不放宽（SEC-01 / P1-D）。
    """
    raw = (settings.git_path_whitelist or "").strip()
    roots = [os.path.realpath(p.strip()) for p in raw.split(",") if p.strip()] if raw else []
    if not roots:
        roots = [os.path.realpath(os.getcwd())]
    # 去重保序（重复条目只影响展示与遍历，不影响判定结果）
    seen: set[str] = set()
    unique = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def check_path_allowed(file_path: str) -> dict:
    """授权项目根校验（P1-D「跨项目 git 根授权」）。

    file 路径先 Path.resolve() 规范化（解析 ../ 与符号链接），再与各授权根的
    规范化路径做 os.path.normcase 归一比较（Windows 大小写不敏感），必须位于
    某个授权根之下（含边界相等）才放行；os.sep 边界比较避免 /app 命中
    /app-secrets。

    返回结构化结果（拒绝不再静默）::

        {"allowed": bool, "reason": str | None, "roots": list[str], "resolved": str}

    reason 仅在拒绝时非空，说明「哪个根都没匹配上」并回显当前生效根列表，
    供日志与 doctor 自检归因。
    """
    roots = get_authorized_roots()
    # resolve 解析 ../ 与符号链接（根内 symlink 指向根外不得绕过）；normcase
    # 在 Windows 上转小写 + 统一分隔符，POSIX 上为恒等，不改变 POSIX 语义
    resolved = os.path.normcase(str(Path(file_path).resolve()))
    for root in roots:
        norm_root = os.path.normcase(root)
        if resolved == norm_root or resolved.startswith(norm_root + os.sep):
            return {"allowed": True, "reason": None, "roots": roots, "resolved": resolved}
    reason = (
        f"路径不在任何授权项目根之下（resolved={resolved}）；"
        f"当前生效根共 {len(roots)} 个: {', '.join(roots)}。"
        "跨项目调试请把目标项目根加入 GIT_PATH_WHITELIST"
    )
    return {"allowed": False, "reason": reason, "roots": roots, "resolved": resolved}


def _is_allowed(file_path: str) -> bool:
    """兼容旧调用方的布尔门（SEC-01）；结构化校验与拒绝原因见 check_path_allowed。"""
    return check_path_allowed(file_path)["allowed"]


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
    check = check_path_allowed(file_path)
    if not check["allowed"]:
        logger.warning("git blame 被授权根拒绝: %s（%s）", file_path, check["reason"])
        return None

    line_no = int(line_no)

    path = Path(file_path)
    if not path.exists():
        return None

    out = _git_cmd(["blame", "-L", f"{line_no},{line_no}", "--porcelain", "--", str(path)], path.parent)
    if not out:
        return None

    parsed = _parse_blame_line(out)
    if not parsed:
        return None

    return {"file": file_path, "line": line_no, **parsed}


def get_recent_diff(file_path: str, commits_back: int = 3) -> Optional[dict]:
    """返回指定文件最近 N 次 commit 的 diff。"""
    check = check_path_allowed(file_path)
    if not check["allowed"]:
        logger.warning("git diff 被授权根拒绝: %s（%s）", file_path, check["reason"])
        return None

    commits_back = max(1, min(int(commits_back), _MAX_COMMITS_BACK))

    path = Path(file_path)
    if not path.exists():
        return None

    out = _git_cmd(["diff", f"HEAD~{commits_back}", "--", str(path)], path.parent)
    if not out:
        return None

    return {"file": file_path, "commits_back": commits_back, "diff": out}

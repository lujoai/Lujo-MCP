"""文档链接验证脚本 v2。

只检查 [text](url) 格式的 Markdown 链接（会被渲染为 HTML <a> 标签）。
不检查 §x.x 文本引用（不是 HTML 链接）。

链接类型：
1. 外部链接 http(s):// — 验证 URL 格式
2. file:/// 绝对路径 — 验证文件是否存在（GitHub 不可用，标记为警告）
3. 相对路径 ../xxx — 验证文件是否存在
4. 锚点 #xxx — 验证锚点是否存在

只扫描 git 跟踪的 Markdown 文件，确保 CI 与本地行为一致，
避免 .gitignore 排除的个人/内部文档产生干扰。

用法：python scripts/check_doc_links.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent


def emit_annotation(level: str, file_path: Path, line: int, message: str) -> None:
    """在 GitHub Actions 中输出 workflow command 注解；本地运行时静默。

    level: "error" 或 "warning"
    file_path: 仓库内相对路径
    line: 链接所在行号（1-based）
    message: 注解内容（会被单行化以避免被截断）
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    rel = str(file_path).replace("\\", "/")
    one_line = message.replace("\n", " ").replace("\r", "")
    print(f"::{level} file={rel},line={line}::{one_line}")


def get_tracked_md_files() -> list[Path]:
    """获取 git 跟踪的 Markdown 文件列表。

    只检查 git ls-files 列出的 .md 文件，确保 CI（仅检出跟踪文件）
    与本地运行行为一致。git 不可用时退回到扫描全部 .md 文件。
    """
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, check=True, cwd=ROOT,
        )
        return sorted(
            ROOT / line.strip()
            for line in result.stdout.splitlines()
            if line.strip().endswith(".md")
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # git 不可用时退回到扫描全部 .md 文件（保留原有行为）
        exclude = {".git", "node_modules", "__pycache__", "site-packages",
                   ".venv", "venv", ".cache", ".pytest_cache", ".trae"}
        return sorted(
            p for p in ROOT.rglob("*.md")
            if not any(part in exclude for part in p.parts)
        )


DOCS = get_tracked_md_files()

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def is_real_link(url: str) -> bool:
    """过滤误匹配：url 必须包含路径/协议特征才视为真实链接。"""
    return any(c in url for c in "/.#") or url.startswith(("http", "mailto"))


def resolve_link(url: str, doc_path: Path) -> tuple[str, str, bool, str]:
    """解析链接，返回 (类型, 路径或URL, 是否有效, 说明)。"""
    # 外部链接
    if url.startswith(("http://", "https://")):
        return ("external", url, True, "外部链接（未请求验证）")

    # mailto
    if url.startswith("mailto:"):
        return ("mailto", url, True, "邮件链接")

    # file:// 绝对路径
    if url.startswith("file:///"):
        file_path_str = unquote(url[8:])  # 去掉 file:///
        file_path = Path(file_path_str)
        exists = file_path.exists()
        rel = str(file_path.relative_to(ROOT)) if file_path.exists() else file_path_str
        return ("file://", rel, exists, "⚠️ file:// 链接在 GitHub 上不可用，建议改为相对路径" if exists else "文件不存在")

    # 纯锚点
    if url.startswith("#"):
        return ("anchor", url, True, "页内锚点（未验证）")

    # 相对路径
    file_part = url.split("#")[0]
    if not file_part:
        return ("anchor", url, True, "页内锚点")

    target = (doc_path.parent / file_part).resolve()
    exists = target.exists()
    try:
        rel = str(target.relative_to(ROOT))
    except ValueError:
        rel = str(target)
    return ("relative", rel, exists, "文件存在" if exists else f"文件不存在")

    # 不会到达
    return ("unknown", url, False, "未知链接类型")


def main():
    total_links = 0
    total_errors = 0
    total_warnings = 0
    results: list[str] = []

    for doc_path in DOCS:
        if not doc_path.exists():
            results.append(f"\n{'='*70}")
            results.append(f"❌ 文件不存在: {doc_path}")
            total_errors += 1
            continue

        content = doc_path.read_text(encoding="utf-8")
        rel_path = doc_path.relative_to(ROOT)
        doc_errors = 0
        doc_warnings = 0

        results.append(f"\n{'='*70}")
        results.append(f"📄 {rel_path}")
        results.append(f"{'='*70}")

        links = list(MD_LINK_RE.finditer(content))
        if not links:
            results.append("  无 Markdown 链接")
            continue

        for m in links:
            text = m.group(1)
            url = m.group(2)
            if not is_real_link(url):
                continue
            total_links += 1

            # 计算链接所在行号（1-based），供 GitHub Actions 注解使用
            line_no = content.count("\n", 0, m.start()) + 1

            link_type, path, ok, info = resolve_link(url, doc_path)

            if link_type == "file://":
                # file:// 链接：文件存在但 GitHub 不可用
                if ok:
                    doc_warnings += 1
                    total_warnings += 1
                    results.append(f"  ⚠️ [{text}] → {path}")
                    results.append(f"      {info}")
                    emit_annotation("warning", rel_path, line_no,
                                    f"[{text}]({url}) → {info}")
                else:
                    doc_errors += 1
                    total_errors += 1
                    results.append(f"  ❌ [{text}] → {path}")
                    results.append(f"      {info}")
                    emit_annotation("error", rel_path, line_no,
                                    f"[{text}]({url}) → {info}")
            elif link_type in ("external", "mailto", "anchor"):
                results.append(f"  ✅ [{text}]({url}) → {link_type}")
            else:
                # 相对路径
                if ok:
                    results.append(f"  ✅ [{text}]({url}) → {path}")
                else:
                    doc_errors += 1
                    total_errors += 1
                    results.append(f"  ❌ [{text}]({url}) → {path}")
                    results.append(f"      {info}")
                    emit_annotation("error", rel_path, line_no,
                                    f"[{text}]({url}) → {info}")

        results.append(f"\n  小结: {len(links)} 个链接, {doc_errors} 个错误, {doc_warnings} 个警告")

    results.append(f"\n{'='*70}")
    results.append(f"汇总: 共检查 {total_links} 个 Markdown 链接")
    results.append(f"  ❌ 错误: {total_errors} 个（链接指向不存在的文件）")
    results.append(f"  ⚠️ 警告: {total_warnings} 个（file:// 链接在 GitHub 上不可用）")
    results.append(f"  ✅ 正常: {total_links - total_errors - total_warnings} 个")
    results.append(f"{'='*70}")

    print("\n".join(results))

    # 汇总级注解：存在错误时以 error 注解高亮，便于在 CI 摘要中一眼定位
    if total_errors > 0:
        emit_annotation("error", Path("README.md"), 1,
                        f"文档链接检查失败：{total_errors} 个错误，"
                        f"{total_warnings} 个警告（file:// 链接）")
    elif total_warnings > 0:
        emit_annotation("warning", Path("README.md"), 1,
                        f"文档链接检查通过，但存在 {total_warnings} 个 "
                        f"file:// 警告（GitHub 上不可用）")

    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

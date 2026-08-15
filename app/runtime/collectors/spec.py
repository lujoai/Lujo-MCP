"""
规范采集器 —— 自动扫描项目中的规范文件，并匹配与报错文件相关的规范片段。

目标：让 AI 在分析错误时自动看到项目约定，减少用户手动复制规范到 prompt。
按 proj1 架构重写（非复制 proj2）：dict in / dict out，复用 redaction 脱敏，带缓存。
"""
import os
import time
import logging
from pathlib import Path
from typing import Optional

from app.runtime.core.redaction import redact

logger = logging.getLogger("lujo-mcp.collectors.spec")

# 显式优先识别的规范文件名
SPEC_CANDIDATES = [
    "CONVENTION.md",
    "API_SPEC.md",
    "COMPONENT_SPEC.md",
    "STYLE_GUIDE.md",
    "README.md",
    ".cursorrules",
]

# 按扩展名/后缀识别规范文件
SPEC_SUFFIXES = (".md", ".cursorrules", "_spec.json", "_spec.yaml", "_spec.yml")

# 跳过目录
_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "dist", "build", ".tox", ".pytest_cache", ".mypy_cache",
    ".next", ".nuxt", "coverage", "target", "vendor",
    ".trae", ".idea", ".vscode", "reference",
}

# 文件名/标题关键词 -> (标签, 目标扩展名)
_TAG_RULES = [
    ({"api", "rest", "http", "endpoint", "swagger", "openapi"},
     ["api", "backend"], [".py", ".ts", ".js", ".java", ".go", ".rs"]),
    ({"component", "ui", "vue", "react", "frontend", "jsx", "tsx", "svelte"},
     ["ui", "frontend"], [".vue", ".tsx", ".jsx", ".svelte", ".html"]),
    ({"style", "css", "scss", "less", "tailwind", "styled"},
     ["style"], [".css", ".scss", ".less", ".styl"]),
    ({"python", "django", "flask", "fastapi", "backend"},
     ["backend", "python"], [".py"]),
    ({"database", "db", "sql", "orm", "prisma", "migration"},
     ["db"], [".py", ".sql", ".prisma"]),
    ({"convention", "guide", "rule", "standard", "cursor"},
     ["general"], []),
]

_MAX_FILE_BYTES = 1024 * 1024  # 1MB
_CHUNK_MAX_CHARS = 800
_TOTAL_MAX_CHARS = 6000  # 约 2000 tokens

# 进程内缓存（按 project_root）
_spec_cache: dict = {"project_root": None, "specs": [], "mtime": 0}


def _find_project_root(file_path: str | Path) -> Path:
    """从文件路径向上查找项目根（含 .git/pyproject.toml/package.json），不超过用户主目录。"""
    path = Path(file_path).resolve()
    file_parent = path.parent if (path.is_file() or path.suffix) else path

    home = Path.home()
    for parent in [file_parent, *file_parent.parents]:
        if not str(parent).startswith(str(home)):
            break
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists() or (parent / "package.json").exists():
            return parent
    return file_parent


def _extract_tags(path: Path, first_lines: str) -> tuple[list[str], list[str]]:
    """根据文件名和开头内容提取标签与目标扩展名。"""
    text = f"{path.name.lower()}\n{first_lines.lower()}"
    tags: set[str] = set()
    extensions: set[str] = set()
    for keywords, rule_tags, rule_exts in _TAG_RULES:
        if any(kw in text for kw in keywords):
            tags.update(rule_tags)
            extensions.update(rule_exts)
    if path.name == ".cursorrules":
        tags.add("general")
    if path.name.lower().startswith("readme") and not tags:
        tags.add("general")
    return sorted(tags), sorted(extensions)


def _slice_content(content: str) -> tuple[str, str]:
    """按二级标题切分，取前几个 chunk，返回 (summary, sliced_content)。"""
    lines = content.splitlines()
    summary = lines[0].strip() if lines else ""
    if summary.startswith("# "):
        summary = summary[2:].strip()
    elif summary.startswith("## "):
        summary = summary[3:].strip()

    chunks, current = [], []
    for line in lines:
        if line.startswith("## "):
            if current:
                chunks.append("\n".join(current))
                current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current))
    if not chunks:
        chunks = [content]

    selected, total = [], 0
    for chunk in chunks[:3]:
        if len(chunk) > _CHUNK_MAX_CHARS:
            chunk = chunk[:_CHUNK_MAX_CHARS] + "\n...（已截断）"
        if total + len(chunk) > _TOTAL_MAX_CHARS:
            remaining = _TOTAL_MAX_CHARS - total
            if remaining > 100:
                selected.append(chunk[:remaining] + "\n...（已截断）")
            break
        selected.append(chunk)
        total += len(chunk)
    return summary, "\n\n".join(selected)


def parse_spec_file(path: Path) -> Optional[dict]:
    """解析单个规范文件为 dict。"""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        content = path.read_text(encoding="utf-8", errors="ignore")
        if not content.strip():
            return None
        first_lines = "\n".join(content.splitlines()[:30])
        tags, target_extensions = _extract_tags(path, first_lines)
        summary, sliced = _slice_content(content)
        # 入库前脱敏
        sliced = redact(sliced) or sliced
        summary = redact(summary) or summary
        return {
            "file": str(path.resolve()),
            "summary": summary,
            "content": sliced,
            "tags": tags,
            "target_extensions": target_extensions,
        }
    except Exception:
        return None


def discover_spec_files(project_root: str | Path) -> list[Path]:
    """扫描项目根目录下的规范文件。"""
    root = Path(project_root)
    if not root.exists():
        return []
    found: set[Path] = set()
    for name in SPEC_CANDIDATES:
        candidate = root / name
        if candidate.exists() and candidate.is_file():
            found.add(candidate.resolve())
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        name = path.name.lower()
        if name in {c.lower() for c in SPEC_CANDIDATES}:
            found.add(path.resolve())
            continue
        if name.endswith(SPEC_SUFFIXES):
            found.add(path.resolve())
    return sorted(found)


def _load_specs(project_root: str | Path) -> list[dict]:
    return [s for s in (parse_spec_file(f) for f in discover_spec_files(project_root)) if s]


def _cache_needs_refresh(project_root: Path) -> bool:
    if _spec_cache["project_root"] != str(project_root):
        return True
    try:
        files = discover_spec_files(project_root)
        if not files:
            return _spec_cache["specs"] != []
        return max(os.path.getmtime(f) for f in files) > _spec_cache["mtime"]
    except Exception:
        return False


def get_project_specs(project_root: Optional[str | Path] = None) -> list[dict]:
    """获取项目规范列表，带缓存。"""
    global _spec_cache
    root = Path(project_root) if project_root else Path.cwd()
    if not root.exists():
        return []
    if _cache_needs_refresh(root):
        _spec_cache = {
            "project_root": str(root),
            "specs": _load_specs(root),
            "mtime": time.time(),
        }
    return _spec_cache["specs"]


def reload_specs(project_root: Optional[str | Path] = None) -> list[dict]:
    """强制刷新规范缓存。"""
    global _spec_cache
    _spec_cache["mtime"] = 0
    return get_project_specs(project_root)


def match_specs(error_file: str, specs: list[dict], max_chars: int = _TOTAL_MAX_CHARS) -> list[dict]:
    """根据报错文件扩展名匹配相关规范，按重要性排序并裁剪总长度。"""
    ext = Path(error_file).suffix.lower()
    matched = [s for s in specs if ext in [e.lower() for e in s["target_extensions"]]]
    matched.extend(s for s in specs if "general" in s["tags"] and s not in matched)

    result, total = [], 0
    for spec in matched:
        length = len(spec["content"])
        if total + length > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                result.append({**spec, "content": spec["content"][:remaining] + "\n...（已截断）"})
            break
        result.append(spec)
        total += length
    return result


def get_related_specs(file_path: str, project_root: Optional[str | Path] = None) -> list[dict]:
    """获取与指定文件相关的项目规范片段。"""
    if project_root is None:
        project_root = _find_project_root(file_path)
    specs = get_project_specs(project_root)
    if not specs:
        return []
    return match_specs(file_path, specs)

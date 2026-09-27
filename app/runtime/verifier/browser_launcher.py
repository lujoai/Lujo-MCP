"""浏览器回退链探测（v0.9.8「浏览器能力进冻结包」）。

冻结发行版自带 playwright Python 库与 node driver，但**不**随包分发
chromium 浏览器二进制（体积 150MB+，`playwright install` 在冻结二进制内
无法执行）。verify_ui / auto_test 因此按下列顺序探测可用浏览器：

  ① playwright 管理的 chromium（源码环境 `playwright install chromium` 装的）
  ② 系统 Chrome   -> launch(channel="chrome")
  ③ 系统 Edge     -> launch(channel="msedge")（Windows 惯例必有）

探测只做**轻量路径存在性检查**：不 launch、不 spawn playwright driver，
同步/异步两个上下文都可安全调用。结果进程内缓存一次（heavy 工具在独立
子进程执行，缓存生命周期 = 单次工具调用）。

resolve_launch_kwargs() 返回：
  {"executable_path": "..."}     —— ① 命中
  {"channel": "chrome"|"msedge"} —— ②③ 命中
  None                           —— 全部缺失，调用方回落 CAPABILITY_MISSING

仅依赖标准库；playwright 为可选依赖（缺失时只影响 ①）。
"""

import json
import os
import shutil
import sys
from pathlib import Path

__all__ = ["resolve_launch_kwargs", "reset_launch_cache"]

# Windows 下系统浏览器的固定安装相对路径（Playwright channel 同款解析位置）
_CHROME_REL = ("Google", "Chrome", "Application", "chrome.exe")
_EDGE_REL = ("Microsoft", "Edge", "Application", "msedge.exe")


# ── ① playwright 管理的 chromium ──


def _browsers_json_path() -> Path | None:
    """playwright 包内 driver 的 browsers.json（冻结产物经 collect_data_files 同样落位）。"""
    try:
        import playwright
    except ImportError:
        return None
    file = getattr(playwright, "__file__", None)
    if not file:
        return None
    return Path(file).parent / "driver" / "package" / "browsers.json"


def _browsers_registry_dir() -> Path | None:
    """playwright 浏览器注册目录（与 playwright 自身解析规则一致）。"""
    override = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if override and override != "0":
        return Path(override)
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _chromium_exe_relative() -> tuple[tuple[str, ...], ...]:
    """chromium 解包目录内可执行文件的候选相对路径（旧版 chrome-win，新版 chrome-win64）。"""
    if sys.platform == "win32":
        return (("chrome-win64", "chrome.exe"), ("chrome-win", "chrome.exe"))
    if sys.platform == "darwin":
        return (("chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),)
    return (("chrome-linux", "chrome"),)


def _probe_managed_chromium() -> str | None:
    """① playwright 管理的 chromium 可执行文件路径；未安装/库缺失返回 None。"""
    registry = _browsers_registry_dir()
    if registry is None:
        return None

    revision = None
    browsers_json = _browsers_json_path()
    if browsers_json is not None:
        try:
            data = json.loads(browsers_json.read_text(encoding="utf-8"))
            revision = next(
                (
                    b.get("revision")
                    for b in data.get("browsers", [])
                    if b.get("name") == "chromium"
                ),
                None,
            )
        except Exception:
            revision = None  # browsers.json 缺失/损坏 -> 走 glob 兜底

    if revision is not None:
        for rel in _chromium_exe_relative():
            candidate = registry / f"chromium-{revision}" / Path(*rel)
            if candidate.is_file():
                return str(candidate)

    # 兜底：revision 无法确定时扫注册目录（取字典序最新的 chromium-*）
    try:
        for browser_dir in sorted(registry.glob("chromium-*"), reverse=True):
            for rel in _chromium_exe_relative():
                candidate = browser_dir / Path(*rel)
                if candidate.is_file():
                    return str(candidate)
    except OSError:
        pass
    return None


# ── ②③ 系统浏览器 ──


def _win_env_candidates(rel: tuple[str, ...], env_names: tuple[str, ...]) -> list[Path]:
    candidates = []
    for env in env_names:
        root = os.environ.get(env)
        if root:
            candidates.append(Path(root).joinpath(*rel))
    return candidates


def _system_chrome_candidates() -> list[Path]:
    if sys.platform == "win32":
        return _win_env_candidates(
            _CHROME_REL, ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")
        )
    candidates = []
    for name in ("google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    return candidates


def _system_edge_candidates() -> list[Path]:
    if sys.platform == "win32":
        # Windows 惯例：Edge 装在 ProgramFiles(x86)，ProgramFiles 兜底
        return _win_env_candidates(
            _EDGE_REL, ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")
        )
    candidates = []
    for name in ("microsoft-edge", "microsoft-edge-stable"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    return candidates


# ── 回退链与缓存 ──


def _probe_launch_kwargs() -> dict | None:
    """按 ①chromium → ②系统 Chrome → ③系统 Edge 顺序探测（轻量路径检查）。"""
    managed = _probe_managed_chromium()
    if managed:
        return {"executable_path": managed}
    if any(p.is_file() for p in _system_chrome_candidates()):
        return {"channel": "chrome"}
    if any(p.is_file() for p in _system_edge_candidates()):
        return {"channel": "msedge"}
    return None


_launch_kwargs_cache: dict | None = None
_cache_ready: bool = False


def resolve_launch_kwargs() -> dict | None:
    """返回 chromium.launch() 需要的浏览器定位参数；无可用浏览器返回 None。

    结果进程内缓存：探测只做路径存在性检查，但 doctor / verify_ui / auto_test
    可能多次询问，缓存避免重复磁盘扫描。
    """
    global _launch_kwargs_cache, _cache_ready
    if not _cache_ready:
        _launch_kwargs_cache = _probe_launch_kwargs()
        _cache_ready = True
    return _launch_kwargs_cache


def reset_launch_cache() -> None:
    """清空探测缓存（测试 / 诊断用；正常运行无需调用）。"""
    global _launch_kwargs_cache, _cache_ready
    _launch_kwargs_cache = None
    _cache_ready = False

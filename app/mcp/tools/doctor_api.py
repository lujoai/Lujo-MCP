"""MCP 工具：doctor —— 运行能力与配置自检（P0-B「浏览器能力可见性」方案 A）。

轻量常驻工具：不启动浏览器、不 spawn 子进程，只做导入探测 / 配置回显。
供宿主智能体在调用 verify_ui / auto_test 前判断浏览器采集能力是否可用，
以及排查 heavy worker 入口、HTTP 监听、UI URL 访问策略、KB 持久化、
vector/embedding 等运行条件（verify_ui / auto_test 返回 CAPABILITY_MISSING
时，可先用本工具定位缺失项）。

每项自检独立兜底：单项失败只体现为 ``ok:false``，绝不让工具整体抛异常。
载荷无 ``error`` 键 → 按全局失败契约恒为工具成功（isError=false）；
单项能力缺失由 ``ok:false`` 表达，与「工具执行失败」语义分离。
"""

import importlib.util
import sys
from pathlib import Path

DOCTOR_DEF = {
    "name": "doctor",
    "description": (
        "自检当前 Lujo 进程的运行能力与配置，返回逐项 ok/detail 与汇总："
        "Playwright 浏览器采集库、chromium 二进制、heavy worker 入口、"
        "HTTP 监听地址端口、UI URL 访问策略（allowlist/allow_private/回环默认放行）、"
        "KB 持久化路径与开关、vector/embedding 能力。"
        "verify_ui / auto_test 返回 CAPABILITY_MISSING 时，可先用本工具定位缺失项。"
        "本工具只读探测，不启动浏览器、不创建子进程。"
    ),
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}


def _check_playwright_library() -> tuple[bool, str]:
    """① playwright 库可导入（sync + async 双 API）。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:
        return False, (
            f"playwright 库不可导入（{type(e).__name__}: {e}）。"
            "源码版安装: pip install playwright && playwright install chromium"
        )
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as e:
        return False, (
            f"playwright.async_api 不可导入（{type(e).__name__}: {e}），"
            "sync_api 正常——安装疑似不完整"
        )
    return True, "playwright 库可导入（sync + async）"


def _check_chromium_binary() -> tuple[bool, str]:
    """② chromium 二进制可解析（executable_path，不启动浏览器）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright 库未安装，无法解析 chromium 可执行文件路径"
    try:
        with sync_playwright() as pw:
            executable = str(pw.chromium.executable_path)
    except Exception as e:
        return False, (
            f"chromium 可执行文件解析失败（{type(e).__name__}: {e}）。"
            "请执行: playwright install chromium"
        )
    if executable and Path(executable).exists():
        return True, f"chromium 可执行文件存在: {executable}"
    return False, (
        f"chromium 可执行文件缺失: {executable or '未知'}。"
        "请执行: playwright install chromium"
    )


def _check_heavy_worker_entry() -> tuple[bool, str]:
    """③ heavy worker 通路：仅检查 --lujo-heavy-worker 入口存在性，不真 spawn。"""
    entry = "app.mcp.protocol.heavy_worker_entry"
    try:
        spec = importlib.util.find_spec(entry)
    except Exception as e:
        return False, f"heavy worker 入口模块定位失败（{type(e).__name__}: {e}）"
    if spec is None:
        return False, (
            f"heavy worker 入口模块 {entry} 不存在，"
            "重型工具（verify_ui/auto_test 等）将无法派发"
        )
    frozen = bool(getattr(sys, "frozen", False))
    mode = (
        "冻结产物（entry_stdio 分流）"
        if frozen
        else "源码（python -m app.mcp.protocol.heavy_worker_entry）"
    )
    return True, f"heavy worker 入口存在（{mode}，worker flag=--lujo-heavy-worker），未实际 spawn"


def _check_http_listen() -> tuple[bool, str]:
    """④ HTTP 监听地址端口（settings 配置回显，不做真实 bind）。"""
    from app.config import settings

    host, port = settings.host, settings.port
    detail = f"HTTP 监听 {host}:{port}"
    if host in ("", "0.0.0.0", "::"):
        detail += "（通配地址：对外网可达，请确认已按 SEC-03 配置 API_KEY）"
    else:
        detail += "（仅该地址可达）"
    return True, detail


def _check_ui_url_policy() -> tuple[bool, str]:
    """⑤ UI URL 白名单回显（allowlist / allow_private / 回环默认放行规则名）。"""
    from app.config import settings

    allowlist = [h.strip() for h in (settings.ui_url_allowlist or "").split(",") if h.strip()]
    detail = (
        f"UI_URL_ALLOWLIST={allowlist if allowlist else '（空）'}；"
        f"UI_URL_ALLOW_PRIVATE={settings.ui_url_allow_private}；"
        "未命中白名单时回环地址默认放行（rule=loopback_default），"
        "非回环私网/元数据地址默认拒绝（rule=private_network），"
        "公网地址按公网校验（rule=public_network）"
    )
    return True, detail


def _check_kb_persistence() -> tuple[bool, str]:
    """⑥ KB 持久化路径与开关状态。"""
    from app.config import settings

    if not settings.kb_persist_enabled:
        return True, "KB 持久化已关闭（KB_PERSIST_ENABLED=false，知识库仅驻内存）"
    explicit = str(settings.kb_persist_path or "").strip()
    if explicit:
        return True, f"KB 持久化已启用，路径: {explicit}"
    try:
        from app.runtime.core.storage.sqlite_kb_store import get_default_kb_persist_path

        return True, f"KB 持久化已启用，使用默认路径: {get_default_kb_persist_path()}"
    except Exception as e:
        return False, f"KB 持久化默认路径解析失败（{type(e).__name__}: {e}）"


def _check_vector_embedding() -> tuple[bool, str]:
    """⑦ vector/embedding 能力（openai_api_key 有无）。"""
    from app.config import settings

    if (settings.openai_api_key or "").strip():
        return True, "已配置 OPENAI_API_KEY，vector/embedding 能力可用"
    return False, (
        "未配置 OPENAI_API_KEY，vector/embedding 能力不可用"
        "（仅影响向量检索增强，不影响核心采集/查询）"
    )


# 自检项注册表：name 即载荷中的稳定标识，供宿主按名定位缺失能力
_CHECKS = (
    ("playwright_library", _check_playwright_library),
    ("chromium_binary", _check_chromium_binary),
    ("heavy_worker_entry", _check_heavy_worker_entry),
    ("http_listen", _check_http_listen),
    ("ui_url_policy", _check_ui_url_policy),
    ("kb_persistence", _check_kb_persistence),
    ("vector_embedding", _check_vector_embedding),
)


def doctor_handler(arguments: dict) -> dict:
    """doctor 工具处理函数：逐项自检，单项失败不炸整体。"""
    checks = []
    for name, probe in _CHECKS:
        try:
            ok, detail = probe()
        except Exception as e:  # 自检兜底：任何探测异常都降级为该单项 ok:false
            ok, detail = False, f"自检项执行异常（{type(e).__name__}: {e}）"
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    ok_count = sum(1 for c in checks if c["ok"])
    return {
        "checks": checks,
        "summary": {"ok_count": ok_count, "fail_count": len(checks) - ok_count},
    }

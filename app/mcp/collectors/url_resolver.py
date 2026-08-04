"""URL Resolver —— 无堆栈场景下通过 HTTP 方法+路径反查 FastAPI 路由表定位 handler 源码。

v0.4.0 M3 引入。静默失败（无异常堆栈）场景下，无法用堆栈帧定位故障函数，
本模块利用 FastAPI 路由表，把请求 (method, path) 映射到 handler 端点，
再解析其源码文件与函数名，供 StaticAnalyzer 做函数级静态分析。

设计原则：
- 纯函数、零副作用（不触发 app 初始化副作用；仅读取已加载的 app.routes）
- 失败返回 None，静默降级，不阻断主流程
- 路径匹配：支持路径参数（如 /debug/{request_id}），exact 优先，模板兜底
"""

from __future__ import annotations

import inspect
import logging
import re
from typing import Any, Optional
from fastapi.routing import APIRoute

logger = logging.getLogger("ai-debug-mcp.mcp.collectors.url_resolver")

# 把 FastAPI 路径模板转成可匹配具体路径的正则（{param} → [^/]+）
_PATH_PARAM_RE = re.compile(r"\{[^}]*\}")


def _path_to_regex(template: str) -> re.Pattern:
    """把 FastAPI 路径模板（含 {param}）编译为匹配具体路径的正则。"""
    pattern = _PATH_PARAM_RE.sub(r"[^/]+", template)
    return re.compile(rf"^{pattern}/?$")


def resolve(method: str, path: str) -> Optional[dict[str, Any]]:
    """按 HTTP 方法 + 路径解析 handler 的源码位置。

    Args:
        method: HTTP 方法（GET/POST/PUT/DELETE...，大小写不敏感）
        path: 请求路径（如 /debug/abc-123）

    Returns:
        {"file": str, "function": str, "module": str} 或 None（未命中/失败）
    """
    if not method or not path:
        return None
    method = method.upper()
    try:
        from app.main import app
    except Exception:
        logger.warning("URL Resolver: 无法加载 app.main", exc_info=True)
        return None

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if method not in (route.methods or []):
            continue
        # 精确匹配优先
        if route.path == path:
            return _describe_endpoint(route.endpoint)
        # 路径模板兜底（含路径参数）
        try:
            if _path_to_regex(route.path).match(path):
                return _describe_endpoint(route.endpoint)
        except Exception:
            continue
    return None


def _describe_endpoint(endpoint: Any) -> Optional[dict[str, Any]]:
    """解析 endpoint 的源码文件、函数名与模块。"""
    try:
        module = inspect.getmodule(endpoint)
        file_path = inspect.getsourcefile(endpoint)
        return {
            "file": file_path or "",
            "function": getattr(endpoint, "__name__", ""),
            "module": module.__name__ if module else "",
        }
    except Exception:
        logger.warning("URL Resolver: 解析 endpoint 失败", exc_info=True)
        return None
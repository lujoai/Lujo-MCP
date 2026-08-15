"""MCP Source Map 工具 —— 前端 minified 堆栈还原（v0.5.1 SM3）。

Agent 可直接调用：入参 frames（浏览器堆栈帧数组）+ 可选 artifact 标识，
出参为还原后的原始源码帧 + 源码片段（sourcesContent 优先，白名单本地文件兜底）。
受 SOURCEMAP_ENABLED 总开关控制（默认关闭；关闭时返回 error 字典，不抛异常）。
"""

from __future__ import annotations

import logging

from app.config import settings
from app.runtime.collectors.sourcemap_store import resolve_frames_auto

logger = logging.getLogger("ai-debug-mcp.tools.sourcemap")

TOOL_DEF = {
    "name": "resolve_stack",
    "description": (
        "用 Source Map 把前端 minified JS 堆栈帧还原为原始源码位置与源码片段"
        "（需 SOURCEMAP_ENABLED=true，且已通过 POST /api/debug/sourcemap 上传 .map，"
        "或按 SOURCEMAP_PATH_PREFIX 约定放置 <前缀>/<文件名>.map）"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "frames": {
                "type": "array",
                "description": "堆栈帧数组，每帧含 file/line/column/function（column 为 0-based）",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "帧文件（可为 URL 或相对路径）"},
                        "line": {"type": "integer", "description": "1-based 行号"},
                        "column": {"type": "integer", "description": "0-based 列号（minified 精确定位必需）"},
                        "function": {"type": "string", "description": "压缩后函数名"},
                    },
                },
            },
            "artifact": {
                "type": "string",
                "description": "可选 artifact 标识（如 app.9f3b2c.js），优先于帧文件名匹配",
            },
        },
        "required": ["frames"],
    },
}


def handler(arguments: dict) -> dict:
    """MCP 工具 handler：还原 minified 堆栈帧。"""
    frames = arguments.get("frames")
    if not isinstance(frames, list) or not frames:
        return {"error": "frames 必须是非空数组"}
    if not settings.sourcemap_enabled:
        return {"error": "sourcemap resolution disabled (SOURCEMAP_ENABLED=false)"}

    try:
        resolved, snippets = resolve_frames_auto(frames, artifact=arguments.get("artifact"))
    except Exception:
        # 与其他采集类工具一致：内部异常降级为 error 字典，绝不抛出
        logger.warning("resolve_stack 解析失败", exc_info=True)
        return {"error": "source map 解析失败（内部错误已降级）", "frames": frames}

    resolved_count = sum(
        1 for f in resolved if isinstance(f, dict) and f.get("resolved")
    )
    return {
        "resolved_frames": resolved,
        "code_snippets": snippets,
        "resolved_count": resolved_count,
        "total_frames": len(resolved),
    }

"""
MCP 工具：get_related_specs —— 根据文件路径返回相关项目规范片段。

AI 在给出修复建议前应参考这些规范，确保方案符合项目约定。
"""
from app.runtime.collectors.spec import get_related_specs

RELATED_SPECS_DEF = {
    "name": "get_related_specs",
    "description": (
        "根据文件路径返回相关的项目规范片段（API 规范、组件规范、代码风格等）。"
        "需要 file；在给出修复建议前调用，确保方案符合项目约定；"
        "与规范/约定无关的纯运行时排错优先调用 diagnose_issue。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "要查询规范的文件路径"},
        },
        "required": ["file"],
    },
}


def tool_get_related_specs(file: str) -> dict:
    specs = get_related_specs(file)
    return {
        "found": bool(specs),
        "count": len(specs),
        "specs": specs,
    }


def related_specs_handler(arguments: dict) -> dict:
    return tool_get_related_specs(arguments["file"])

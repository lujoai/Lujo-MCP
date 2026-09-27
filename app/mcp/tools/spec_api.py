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
    if not specs:
        # 无数据契约（对齐 diagnose_issue）：next_step 说明空的常见原因与补救。
        # 语义见 app/runtime/collectors/spec.py：虚拟帧/本地不存在的路径直接返回
        # 空；项目根（含 .git/pyproject.toml/package.json 的目录）由帧路径向上
        # 推导；规范文件缺失或其适用扩展名不覆盖该文件类型时匹配结果为空。
        return {
            "found": False,
            "count": 0,
            "specs": [],
            "next_step": (
                "未匹配到项目规范。常见原因：① 传入的不是本地真实存在的文件路径"
                "（浏览器虚拟帧 / 页面 URL 无法定位项目）；② 该文件所在项目根下"
                "没有规范文件（本工具扫描项目根内的 README.md / CONVENTION.md / "
                "API_SPEC.md / *.md / .cursorrules 等，跳过 node_modules / .venv "
                "等目录）；③ 现有规范声明的适用扩展名不覆盖该文件类型。可在目标"
                "项目根放置规范文件后重试。"
            ),
        }
    return {
        "found": True,
        "count": len(specs),
        "specs": specs,
    }


def related_specs_handler(arguments: dict) -> dict:
    return tool_get_related_specs(arguments["file"])

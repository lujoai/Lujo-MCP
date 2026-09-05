"""
MCP 工具：get_blame_for_frame / get_recent_diff。

供宿主 AI 判断错误是否由近期改动引入。安全（超时+白名单）在 core/git 实现。
"""
from app.runtime.core.git import get_blame_for_frame, get_recent_diff


# ── HTTP 侧注册用 TOOL_DEF（M8 注册）──
BLAME_DEF = {
    "name": "get_blame_for_frame",
    "description": (
        "git blame：查询指定文件/行最后一次是谁在哪次 commit 修改的。"
        "需要 file+line（可从 diagnose_issue 返回的堆栈帧取得）；"
        "适合判断报错代码是否由近期改动引入、辅助定位引入者；纯代码问题不要调用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "文件路径"},
            "line": {"type": "integer", "description": "行号"},
        },
        "required": ["file", "line"],
    },
}

RECENT_DIFF_DEF = {
    "name": "get_recent_diff",
    "description": (
        "返回指定文件最近 N 次 commit 的 diff。需要 file；"
        "适合在 diagnose_issue 定位到可疑文件后，对比近期改动找出引入问题的变更；"
        "纯代码问题不要调用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "文件路径"},
            "commits_back": {"type": "integer", "default": 3, "description": "回溯多少 commit，默认 3"},
        },
        "required": ["file"],
    },
}


def tool_get_blame_for_frame(file: str, line: int) -> dict:
    result = get_blame_for_frame(file, line)
    return {"found": result is not None, "blame": result}


def tool_get_recent_diff(file: str, commits_back: int = 3) -> dict:
    result = get_recent_diff(file, commits_back)
    return {"found": result is not None, "diff": result}


# ── MCP handler（供 register_tool 使用）──
def blame_handler(arguments: dict) -> dict:
    return tool_get_blame_for_frame(arguments["file"], arguments["line"])


def recent_diff_handler(arguments: dict) -> dict:
    return tool_get_recent_diff(arguments["file"], arguments.get("commits_back", 3))

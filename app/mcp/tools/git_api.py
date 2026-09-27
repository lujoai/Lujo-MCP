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


# git 工具 found:false 的统一引导（对齐 diagnose_issue 的无数据契约）。
# 底层 core/git 对「白名单拒绝 / 文件不存在 / 非 git 仓库或命令失败超时 /
# 行未提交」统一返回 None，工具层无法区分具体原因，因此 next_step 按可核查
# 顺序列出全部条件（语义见 app/runtime/core/git.py 的 _is_allowed）。
_GIT_EMPTY_NEXT_STEP = (
    "未查到 git 归因结果。按顺序核查：① 本工具仅允许查询 GIT_PATH_WHITELIST "
    "白名单前缀内的文件（该配置为逗号分隔绝对路径；未配置时默认收敛为本服务"
    "进程工作目录，目录外路径一律拒绝），跨项目调试请把目标文件所在目录加入 "
    "GIT_PATH_WHITELIST 后重试；② 确认文件在运行本服务的机器上真实存在且位于"
    "其 git 仓库内。"
)
_BLAME_EMPTY_NEXT_STEP = _GIT_EMPTY_NEXT_STEP + (
    "③ 该行内容可能从未被 commit（未跟踪新行 blame 无归属）。"
)
_DIFF_EMPTY_NEXT_STEP = _GIT_EMPTY_NEXT_STEP + (
    "③ 该文件在最近 commit 中可能没有任何变更。"
)


def tool_get_blame_for_frame(file: str, line: int) -> dict:
    result = get_blame_for_frame(file, line)
    if result is None:
        return {"found": False, "blame": None, "next_step": _BLAME_EMPTY_NEXT_STEP}
    return {"found": True, "blame": result}


def tool_get_recent_diff(file: str, commits_back: int = 3) -> dict:
    result = get_recent_diff(file, commits_back)
    if result is None:
        return {"found": False, "diff": None, "next_step": _DIFF_EMPTY_NEXT_STEP}
    return {"found": True, "diff": result}


# ── MCP handler（供 register_tool 使用）──
def blame_handler(arguments: dict) -> dict:
    return tool_get_blame_for_frame(arguments["file"], arguments["line"])


def recent_diff_handler(arguments: dict) -> dict:
    return tool_get_recent_diff(arguments["file"], arguments.get("commits_back", 3))

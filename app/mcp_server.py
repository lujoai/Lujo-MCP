"""
标准 MCP Server（stdio transport）。

这是 Trae / Codex / Claude Desktop 之类的 MCP 客户端真正会启动的入口，
通过 stdio 管道 + JSON-RPC 协议通信（由 mcp SDK 处理，不需要自己实现协议细节）。

注册方式（在 Trae/Codex 的 MCP 配置里）：
{
  "mcpServers": {
    "ai-debug-mcp": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/绝对路径/ai-debug-mcp"
    }
  }
}

设计原则：这里只暴露"采集数据"的工具（get_stacktrace / get_debug_context /
get_runtime_snapshot / search_logs / list_recent_traces），
不默认做LLM推理 —— 宿主AI（Trae/Codex里的模型）拿到原始数据后自己判断根因，
这样避免重复推理、重复花钱。analyze_with_llm 作为可选工具保留，
仅在宿主客户端本身不具备推理能力时才需要用它。
"""
import asyncio
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from app.mcp.tools.stacktrace_api import get_stacktrace as tool_get_stacktrace
from app.mcp.tools.trace_api import list_recent_traces as tool_list_recent_traces, search_logs as tool_search_logs
from app.mcp.tools.debug_api import (
    get_debug_context as tool_get_debug_context,
    get_runtime_snapshot as tool_get_runtime_snapshot,
    analyze_with_llm as tool_analyze_with_llm,
)
from app.mcp.tools.network_api import tool_ingest_network, tool_get_network_trace
from app.mcp.tools.git_api import tool_get_blame_for_frame, tool_get_recent_diff
from app.mcp.tools.silent_failure_api import tool_ingest_silent_failure
from app.mcp.tools.ingest_api import tool_ingest_error
from app.mcp.tools.spec_api import tool_get_related_specs
from app.mcp.tools.auto_test_api import auto_test_handler
from app.mcp.hooks.exception_hook import install_global_hook

logging.basicConfig(level=logging.INFO, stream=None, force=True)  # stdio模式下不要往stdout打日志，避免污染协议流
logger = logging.getLogger("ai-debug-mcp")

server = Server("ai-debug-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:#async声明函数是可以等待的
    return [
        Tool(
            name="get_stacktrace",
            description=(
                "获取最近一次捕获的异常堆栈，包含每一帧的文件路径、行号、函数名。"
                "不传 trace_id 则返回最新一条。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trace_id": {"type": "string", "description": "可选，指定要查看的追踪ID"}
                },
            },
        ),
        Tool(
            name="get_runtime_snapshot",
            description="获取当前进程运行时快照：CPU占用、内存、线程数、Python版本，用于判断是否是资源类问题。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="search_logs",
            description="按关键字和时间范围（最近N分钟）搜索历史追踪记录。",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键字，匹配异常类型或异常消息"},
                    "since_minutes": {"type": "integer", "default": 30, "description": "只搜索最近N分钟内的记录"},
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="get_debug_context",
            description=(
                "【核心工具】一次性获取某次错误的完整调试上下文：异常堆栈 + 运行时快照 + "
                "堆栈每一帧对应的源码片段（自动定位，含可点击的 IDE 链接，无需再单独读取文件）。"
                "推荐宿主AI拿到这份数据后自行分析根因，不需要额外调用 analyze_with_llm。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trace_id": {"type": "string", "description": "可选，指定 error_id；不传则取最新一条捕获记录"}
                },
            },
        ),
        Tool(
            name="list_recent_traces",
            description="列出最近发生的错误追踪记录摘要列表（不含完整堆栈），供AI选择要深入查看哪一条。",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 10}},
            },
        ),
        Tool(
            name="analyze_with_llm",
            description=(
                "【可选工具，一般不需要调用】调用内置LLM对指定追踪记录做根因分析。"
                "仅在当前MCP客户端本身不具备AI推理能力时才使用此工具；"
                "如果你（宿主AI）本身能推理，请直接用 get_debug_context 拿原始数据自行分析，"
                "调用这个会产生重复的LLM调用花费。"
            ),
            inputSchema={
                "type": "object",
                "properties": {"trace_id": {"type": "string"}},
            },
        ),
        Tool(
            name="ingest_network",
            description="单条上报网络请求记录，通常由浏览器 SDK 或中间件调用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "record": {"type": "object", "description": "网络请求记录"},
                    "trace_id": {"type": "string"},
                    "request_id": {"type": "string"},
                },
                "required": ["record"],
            },
        ),
        Tool(
            name="get_network_trace",
            description="查询与某条 trace 关联的所有网络请求记录。",
            inputSchema={
                "type": "object",
                "properties": {"trace_id": {"type": "string"}},
                "required": ["trace_id"],
            },
        ),
        Tool(
            name="get_blame_for_frame",
            description="查询指定文件/行最后一次是谁在哪次 commit 修改的，用于判断错误是不是近期改动引入。",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                },
                "required": ["file", "line"],
            },
        ),
        Tool(
            name="get_recent_diff",
            description="返回指定文件最近 N 次 commit 的 diff，用于对比近期改动。",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "commits_back": {"type": "integer", "default": 3},
                },
                "required": ["file"],
            },
        ),
        Tool(
            name="ingest_silent_failure",
            description=(
                "上报一条前端静默失败：用户期望发生的行为未在指定时间内出现，且没有显式异常。"
                "包含 UI 事件链、网络请求链和期望行为描述。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "frames": {"type": "array", "items": {"type": "object"}},
                    "ui_events": {"type": "array", "items": {"type": "object"}},
                    "network_records": {"type": "array", "items": {"type": "object"}},
                    "expectation": {"type": "object"},
                    "source": {"type": "string", "default": "browser_sdk"},
                    "extra": {"type": "object", "default": {}},
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="ingest_error",
            description=(
                "供任意语言/进程主动上报一条错误（不限于 Python）。"
                "上报后自动脱敏并进入统一调试上下文。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "exc_type": {"type": "string"},
                    "message": {"type": "string"},
                    "frames": {"type": "array", "items": {"type": "object"}},
                    "source": {"type": "string", "default": "ingest"},
                    "extra": {"type": "object", "default": {}},
                },
                "required": ["exc_type", "message"],
            },
        ),
        Tool(
            name="get_related_specs",
            description=(
                "根据文件路径返回相关的项目规范片段（如 API 规范、组件规范、代码风格等）。"
                "AI 在给出修复建议前应参考这些规范，确保方案符合项目约定。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "要查询规范的文件路径"},
                },
                "required": ["file"],
            },
        ),
        Tool(
            name="auto_test",
            description=(
                "自动遍历页面所有可交互元素（按钮/链接/输入框），"
                "依次执行点击并监听控制台错误和网络 4xx/5xx。"
                "不需要手动指定选择器，适合快速验收 AI 生成的前端页面。"
                "需要 Playwright（pip install playwright && playwright install chromium）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要测试的页面 URL"},
                    "max_actions": {"type": "integer", "default": 20},
                    "capture_console": {"type": "boolean", "default": True},
                    "capture_network": {"type": "boolean", "default": True},
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "get_stacktrace":
            result = tool_get_stacktrace(arguments.get("trace_id"))
        elif name == "get_runtime_snapshot":
            result = tool_get_runtime_snapshot()
        elif name == "search_logs":
            result = tool_search_logs(
                keyword=arguments["keyword"],
                since_minutes=arguments.get("since_minutes", 30),
            )
        elif name == "get_debug_context":
            result = tool_get_debug_context(arguments.get("trace_id"))
        elif name == "list_recent_traces":
            result = tool_list_recent_traces(arguments.get("limit", 10))
        elif name == "analyze_with_llm":
            result = tool_analyze_with_llm(arguments.get("trace_id"))
        elif name == "ingest_network":
            result = tool_ingest_network(
                record=arguments.get("record", {}),
                trace_id=arguments.get("trace_id"),
                request_id=arguments.get("request_id"),
            )
        elif name == "get_network_trace":
            result = tool_get_network_trace(arguments["trace_id"])
        elif name == "get_blame_for_frame":
            result = tool_get_blame_for_frame(arguments["file"], arguments["line"])
        elif name == "get_recent_diff":
            result = tool_get_recent_diff(arguments["file"], arguments.get("commits_back", 3))
        elif name == "ingest_silent_failure":
            result = tool_ingest_silent_failure(
                message=arguments.get("message", ""),
                frames=arguments.get("frames"),
                ui_events=arguments.get("ui_events"),
                network_records=arguments.get("network_records"),
                expectation=arguments.get("expectation"),
                source=arguments.get("source", "browser_sdk"),
                extra=arguments.get("extra"),
            )
        elif name == "ingest_error":
            result = tool_ingest_error(
                exc_type=arguments.get("exc_type", "UnknownError"),
                message=arguments.get("message", ""),
                frames=arguments.get("frames", []),
                source=arguments.get("source", "ingest"),
                extra=arguments.get("extra"),
            )
        elif name == "auto_test":
            result = await auto_test_handler(arguments)
        elif name == "get_related_specs":
            result = tool_get_related_specs(arguments["file"])
        else:
            result = {"error": f"未知工具: {name}"}
    except Exception as e:
        logger.exception("工具调用失败: %s", name)
        result = {"error": f"工具执行异常: {e}"}

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def main():
    install_global_hook()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

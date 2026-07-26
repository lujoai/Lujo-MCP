"""MCP 工具注册入口 —— 统一注册所有工具，供 HTTP / stdio 传输复用"""

from app.mcp.protocol.server import register_tool


def register_all_tools():
    """注册全部 MCP 工具（HTTP 与 stdio 传输共用同一份业务逻辑）"""
    from app.mcp.tools.debug_api import TOOL_DEF as debug_tool, handler as debug_handler
    from app.mcp.tools.context_api import TOOL_DEF as context_tool, handler as context_handler
    from app.mcp.tools.trace_api import TOOL_DEF as trace_tool, handler as trace_handler
    from app.mcp.tools.stacktrace_api import TOOL_DEF as stacktrace_tool, handler as stacktrace_handler
    # M3-M7 新增工具
    from app.mcp.tools.network_api import (
        NETWORK_INGEST_DEF, NETWORK_TRACE_DEF,
        ingest_network_handler, get_network_trace_handler,
    )
    from app.mcp.tools.git_api import BLAME_DEF, RECENT_DIFF_DEF, blame_handler, recent_diff_handler
    from app.mcp.tools.silent_failure_api import SILENT_FAILURE_DEF, silent_failure_handler
    from app.mcp.tools.ingest_api import INGEST_ERROR_DEF, ingest_error_handler
    from app.mcp.tools.console_api import INGEST_CONSOLE_DEF, ingest_console_handler
    from app.mcp.tools.spec_api import RELATED_SPECS_DEF, related_specs_handler
    from app.mcp.tools.verify_api import VERIFY_DEF, verify_handler
    from app.mcp.tools.verify_ui_api import VERIFY_UI_DEF, verify_ui_handler
    from app.mcp.tools.auto_test_api import AUTO_TEST_DEF, auto_test_handler
    from app.mcp.tools.repair_api import (
        REPAIR_ASYNC_DEF, REPAIR_RESULT_DEF,
        repair_async_handler, repair_result_handler,
    )

    register_tool(**debug_tool, handler=debug_handler)
    register_tool(**context_tool, handler=context_handler)
    register_tool(**trace_tool, handler=trace_handler)
    register_tool(**stacktrace_tool, handler=stacktrace_handler)
    register_tool(**NETWORK_INGEST_DEF, handler=ingest_network_handler)
    register_tool(**NETWORK_TRACE_DEF, handler=get_network_trace_handler)
    register_tool(**BLAME_DEF, handler=blame_handler)
    register_tool(**RECENT_DIFF_DEF, handler=recent_diff_handler)
    register_tool(**SILENT_FAILURE_DEF, handler=silent_failure_handler)
    register_tool(**INGEST_ERROR_DEF, handler=ingest_error_handler)
    register_tool(**INGEST_CONSOLE_DEF, handler=ingest_console_handler)
    register_tool(**RELATED_SPECS_DEF, handler=related_specs_handler)
    register_tool(**VERIFY_DEF, handler=verify_handler)
    register_tool(**VERIFY_UI_DEF, handler=verify_ui_handler)
    register_tool(**AUTO_TEST_DEF, handler=auto_test_handler)
    register_tool(**REPAIR_ASYNC_DEF, handler=repair_async_handler)
    register_tool(**REPAIR_RESULT_DEF, handler=repair_result_handler)

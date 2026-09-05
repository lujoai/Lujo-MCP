"""MCP Tool Category Metadata 测试（v0.5）。

验证：
1. tools/list 返回包含 category 和 experimental
2. 原有 tool name 不变
3. inputSchema 不变
4. 旧 MCP client 仍可解析（核心字段存在）
5. 分类映射正确
6. experimental 标记正确
"""
import asyncio
import json


from app.mcp.protocol.server import _tool_registry, _handle_tools_list
from app.mcp.tools import register_all_tools
from app.mcp.protocol.jsonrpc import JSONRPCRequest


# ── 1. tools/list 包含 category ──

class TestToolsListContainsCategory:
    """tools/list 响应中每个工具都应包含 category 和 experimental 字段。"""

    def setup_method(self):
        _tool_registry.clear()
        register_all_tools()

    def test_every_tool_has_category(self):
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
        resp = _handle_tools_list(req)
        tools = resp["result"]["tools"]
        for tool in tools:
            assert "category" in tool, f"Tool {tool['name']} missing 'category'"
            assert tool["category"] in ("agent", "sdk"), (
                f"Tool {tool['name']} has invalid category: {tool['category']}"
            )

    def test_every_tool_has_experimental(self):
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
        resp = _handle_tools_list(req)
        tools = resp["result"]["tools"]
        for tool in tools:
            assert "experimental" in tool, f"Tool {tool['name']} missing 'experimental'"
            assert isinstance(tool["experimental"], bool)

    def test_tools_list_json_serializable(self):
        """tools/list 响应必须可 JSON 序列化。"""
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
        resp = _handle_tools_list(req)
        json.dumps(resp, ensure_ascii=False, default=str)


# ── 2. 原有 tool name 不变 ──

class TestToolNamesUnchanged:
    """所有原有 tool name 必须保持不变；新增工具仅允许白名单追加。

    v0.7.3 新增：diagnose_issue（统一诊断入口）/ list_recent_traces /
    search_logs（近期错误查询工具，此前为内部函数）。
    """

    EXPECTED_NAMES = {
        "debug", "context", "trace", "stacktrace",
        "ingest_network", "get_network_trace",
        "get_blame_for_frame", "get_recent_diff",
        "ingest_silent_failure", "ingest_error",
        "ingest_console", "get_related_specs",
        "verify", "verify_ui",
        "auto_test",
        "repair_async", "repair_result",
        "resolve_stack",
        # v0.7.3 统一诊断入口 + 近期错误查询
        "diagnose_issue",
        "list_recent_traces",
        "search_logs",
        # v0.7.5 OpenAPI 一键生成断言规范
        "ingest_specs",
    }

    def setup_method(self):
        _tool_registry.clear()
        register_all_tools()

    def test_all_original_names_present(self):
        registered = set(_tool_registry.keys())
        missing = self.EXPECTED_NAMES - registered
        assert not missing, f"Missing tools: {missing}"

    def test_no_extra_tools_added(self):
        registered = set(_tool_registry.keys())
        extra = registered - self.EXPECTED_NAMES
        assert not extra, f"Unexpected new tools: {extra}"

    def test_tool_count_matches_whitelist(self):
        """注册总数与白名单一致（22 = 21 + v0.7.5 ingest_specs）。"""
        assert len(_tool_registry) == 22


# ── 3. inputSchema 不变 ──

class TestInputSchemaUnchanged:
    """inputSchema 不应被 metadata 修改影响。"""

    def setup_method(self):
        _tool_registry.clear()
        register_all_tools()

    def test_every_tool_has_inputschema(self):
        for name, tool in _tool_registry.items():
            assert "inputSchema" in tool, f"{name} missing inputSchema"
            assert isinstance(tool["inputSchema"], dict)

    def test_tools_list_inputschema_matches_registry(self):
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
        resp = _handle_tools_list(req)
        tools = resp["result"]["tools"]
        for tool in tools:
            registry_tool = _tool_registry[tool["name"]]
            assert tool["inputSchema"] == registry_tool["inputSchema"]


# ── 4. 旧 MCP client 仍可解析 ──

class TestBackwardCompatibility:
    """旧 MCP client 只关注 name/description/inputSchema，应仍可解析。"""

    def setup_method(self):
        _tool_registry.clear()
        register_all_tools()

    def test_core_fields_present(self):
        """旧客户端依赖的 3 个核心字段仍存在。"""
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
        resp = _handle_tools_list(req)
        tools = resp["result"]["tools"]
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool

    def test_old_client_can_ignore_extra_fields(self):
        """模拟旧客户端只提取 name/description/inputSchema。

        v0.7.3: tools/list 只暴露 Agent-facing 工具——4 个 SDK 上报工具
        （agent_visible=False）不进清单；v0.7.5 新增 ingest_specs 后公开数为 18。
        """
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
        resp = _handle_tools_list(req)
        tools = resp["result"]["tools"]
        old_client_view = [
            {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in tools
        ]
        assert len(old_client_view) == 18
        sdk_names = {"ingest_network", "ingest_error", "ingest_console", "ingest_silent_failure"}
        assert {e["name"] for e in old_client_view} & sdk_names == set()
        # 每个条目都是有效的旧格式
        for entry in old_client_view:
            assert set(entry.keys()) == {"name", "description", "inputSchema"}


# ── 5. 分类映射正确 ──

class TestCategoryMapping:
    """验证每个工具的 category 分类正确。"""

    EXPECTED_CATEGORIES = {
        # agent: 查询/分析类
        "debug": "agent",
        "context": "agent",
        "trace": "agent",
        "stacktrace": "agent",
        "get_network_trace": "agent",
        "get_blame_for_frame": "agent",
        "get_recent_diff": "agent",
        "get_related_specs": "agent",
        "ingest_specs": "agent",
        "verify": "agent",
        "verify_ui": "agent",
        # sdk: 数据采集类
        "ingest_network": "sdk",
        "ingest_error": "sdk",
        "ingest_console": "sdk",
        "ingest_silent_failure": "sdk",
        # agent + experimental
        "auto_test": "agent",
        "repair_async": "agent",
        "repair_result": "agent",
        "resolve_stack": "agent",
    }

    def setup_method(self):
        _tool_registry.clear()
        register_all_tools()

    def test_all_categories_match(self):
        for name, expected_cat in self.EXPECTED_CATEGORIES.items():
            tool = _tool_registry[name]
            assert tool["category"] == expected_cat, (
                f"Tool {name}: expected category={expected_cat}, got {tool['category']}"
            )

    def test_sdk_tools_are_ingest_only(self):
        """sdk category 只应包含 ingest_* 工具。"""
        for name, tool in _tool_registry.items():
            if tool["category"] == "sdk":
                assert name.startswith("ingest_"), (
                    f"Tool {name} has category='sdk' but doesn't start with 'ingest_'"
                )


# ── 6. experimental 标记正确 ──

class TestExperimentalFlag:
    """验证 experimental 标记正确。"""

    EXPECTED_EXPERIMENTAL = {
        "auto_test": True,
        "repair_async": True,
        "repair_result": True,
        "resolve_stack": True,
    }

    def setup_method(self):
        _tool_registry.clear()
        register_all_tools()

    def test_experimental_tools_marked(self):
        for name, expected_exp in self.EXPECTED_EXPERIMENTAL.items():
            tool = _tool_registry[name]
            assert tool["experimental"] == expected_exp, (
                f"Tool {name}: expected experimental={expected_exp}, got {tool['experimental']}"
            )

    def test_non_experimental_tools_default_false(self):
        """非实验工具的 experimental 应为 False。"""
        experimental_names = set(self.EXPECTED_EXPERIMENTAL.keys())
        for name, tool in _tool_registry.items():
            if name not in experimental_names:
                assert tool["experimental"] is False, (
                    f"Tool {name} should not be experimental"
                )

    def test_tools_list_reflects_experimental(self):
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
        resp = _handle_tools_list(req)
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        for name, expected_exp in self.EXPECTED_EXPERIMENTAL.items():
            assert tools[name]["experimental"] == expected_exp


# ── 7. stdio transport 也包含 metadata ──

class TestStdioTransportMetadata:
    """stdio transport 的 list_tools() 也应包含 category 和 experimental。"""

    def test_stdio_tools_have_category(self):
        import app.mcp_server as mcp_server
        tools = asyncio.run(mcp_server.list_tools())
        for tool in tools:
            # Tool 对象的 extra fields 可通过 model_extra 访问
            extra = tool.model_extra or {}
            assert "category" in extra, f"stdio tool {tool.name} missing 'category'"
            assert extra["category"] in ("agent", "sdk")

    def test_stdio_tools_have_experimental(self):
        import app.mcp_server as mcp_server
        tools = asyncio.run(mcp_server.list_tools())
        for tool in tools:
            extra = tool.model_extra or {}
            assert "experimental" in extra, f"stdio tool {tool.name} missing 'experimental'"

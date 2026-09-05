"""ingest_specs 工具测试：OpenAPI 一键生成断言规范并入库（规范自动生成最后一环）。

覆盖：
1. 合法 OpenAPI → 解析出断言规范并写入 spec_store（MCP 全链路）
2. store=false → 仅生成草稿不入库
3. 重复调用去重（同 kind+target 不重复入库，防 AI 多次调用刷屏）
4. 错误参数 → 协议层 -32602
5. 无 paths 的 openapi → count=0 不报错
6. 工具注册与 description 自包含
"""
import json

import pytest

from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import _handle_tools_call, _tool_registry
from app.mcp.tools import register_all_tools
from app.runtime.verifier import spec_store


@pytest.fixture(autouse=True)
def _registered_and_clean_specs():
    from app.runtime.core.storage import factory as _storage_factory

    register_all_tools()
    # spec_store.clear() 会重置恢复标志，下一次 list_specs 会从 memory trace
    # 存储回灌旧 spec（跨测试残留）——重置存储单例保证用例独立。
    _storage_factory._trace_store = None
    spec_store.clear()
    yield
    spec_store.clear()
    _storage_factory._trace_store = None


OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "demo", "version": "1.0"},
    "paths": {
        "/api/orders": {
            "get": {"summary": "获取订单列表", "responses": {"200": {"description": "ok"}}},
            "post": {"summary": "创建订单", "responses": {"201": {"description": "created"}}},
        }
    },
}


async def _call_tool(name: str, arguments: dict):
    req = JSONRPCRequest(
        id="ingest-1",
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )
    resp = await _handle_tools_call(req)
    assert resp.get("error") is None, f"协议层报错: {resp.get('error')}"
    return json.loads(resp["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_ingest_specs_parses_and_stores():
    result = await _call_tool("ingest_specs", {"openapi": OPENAPI})

    assert result["count"] == 2
    assert result["stored"] == 2
    assert len(result["spec_ids"]) == 2

    stored = spec_store.list_specs(kind="api")
    targets = {s["target"] for s in stored}
    assert "GET /api/orders" in targets
    assert "POST /api/orders" in targets
    get_spec = next(s for s in stored if s["target"] == "GET /api/orders")
    assert get_spec["expect"]["status"] == 200
    post_spec = next(s for s in stored if s["target"] == "POST /api/orders")
    assert post_spec["expect"]["status"] == 201


@pytest.mark.asyncio
async def test_ingest_specs_draft_only_when_store_false():
    result = await _call_tool("ingest_specs", {"openapi": OPENAPI, "store": False})

    assert result["count"] == 2
    assert result["stored"] == 0
    assert result["spec_ids"] == []
    assert spec_store.list_specs() == []


@pytest.mark.asyncio
async def test_ingest_specs_dedup_on_repeat_calls():
    """AI 多次调用同一 OpenAPI 不应产生重复规范。"""
    first = await _call_tool("ingest_specs", {"openapi": OPENAPI})
    second = await _call_tool("ingest_specs", {"openapi": OPENAPI})

    assert first["stored"] == 2
    assert second["count"] == 2
    assert second["stored"] == 0
    assert second["skipped"] == 2
    assert len(spec_store.list_specs()) == 2


@pytest.mark.asyncio
async def test_ingest_specs_invalid_params_return_invalid_params():
    for bad_args in (
        {},                          # 缺 openapi（required）
        {"openapi": "not-a-dict"},   # 类型错误
        {"openapi": 123},            # 类型错误
    ):
        req = JSONRPCRequest(
            id="ingest-bad",
            method="tools/call",
            params={"name": "ingest_specs", "arguments": bad_args},
        )
        resp = await _handle_tools_call(req)
        assert resp["error"]["code"] == -32602, f"{bad_args} 应返回 -32602"


@pytest.mark.asyncio
async def test_ingest_specs_empty_paths_returns_zero_without_error():
    result = await _call_tool("ingest_specs", {"openapi": {"openapi": "3.0.0", "paths": {}}})

    assert result["count"] == 0
    assert result["stored"] == 0


@pytest.mark.asyncio
async def test_ingest_specs_registered_with_selfcontained_description():
    register_all_tools()
    tool = _tool_registry["ingest_specs"]
    assert tool["category"] == "agent"
    assert "OpenAPI" in tool["description"]
    assert "request_id" in tool["description"]

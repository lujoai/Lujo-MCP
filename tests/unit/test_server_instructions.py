"""单元测试：MCP initialize 响应的 instructions 字段（v0.9.8）。

背景（dogfooding 实证）：宿主 AI 对项目内规则文件不信任，但 MCP 握手下发的
instructions 属于「服务器出厂说明」，信任级别更高；且宿主不知道运行时调试
应先调 diagnose_issue。本文件锁定：

1. stdio 路径（protocol.server.dispatch → _handle_initialize）返回 instructions
2. HTTP 路径（api/mcp_routes → dispatch_raw，同一构造点）返回 instructions
3. instructions 内容含 diagnose_issue / auto_test 调用策略（自包含引导）
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mcp_routes import router
from app.mcp.protocol.jsonrpc import JSONRPCRequest
from app.mcp.protocol.server import dispatch
from app.mcp.transports.session import registry


@pytest.mark.asyncio
async def test_initialize_stdio_result_contains_instructions():
    """stdio/协议层：initialize result 必须含 instructions 且内容含 diagnose_issue。"""
    req = JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={})
    resp = await dispatch(req)
    assert resp.get("error") is None, f"initialize 报错: {resp.get('error')}"
    result = resp["result"]
    # 既有握手字段不受影响（回归保护）
    assert "protocolVersion" in result
    assert "capabilities" in result
    assert "serverInfo" in result
    # 新增字段：内容自包含「先调 diagnose_issue」的策略
    instructions = result.get("instructions")
    assert isinstance(instructions, str) and instructions.strip()
    assert "diagnose_issue" in instructions
    assert "auto_test" in instructions


def test_initialize_http_result_contains_instructions():
    """HTTP：POST /mcp initialize 响应同样携带 instructions（同一构造点）。"""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    instructions = result.get("instructions")
    assert isinstance(instructions, str) and instructions.strip()
    assert "diagnose_issue" in instructions
    assert "auto_test" in instructions
    # 本用例新建的握手会话不残留到其他用例
    sid = resp.headers.get("Mcp-Session-Id")
    if sid:
        registry._sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_initialize_version_negotiation_keeps_instructions():
    """版本协商回退路径（未知 protocolVersion）同样返回 instructions。"""
    req = JSONRPCRequest(
        jsonrpc="2.0", id=2, method="initialize",
        params={"protocolVersion": "1900-01-01"},
    )
    resp = await dispatch(req)
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "diagnose_issue" in resp["result"]["instructions"]


def test_server_instructions_constant_is_stable():
    """常量本身即契约：单行中文字符串，含完整调用链策略关键词。"""
    from app.mcp.protocol.server import SERVER_INSTRUCTIONS

    assert isinstance(SERVER_INSTRUCTIONS, str)
    assert "diagnose_issue" in SERVER_INSTRUCTIONS
    assert "auto_test" in SERVER_INSTRUCTIONS
    assert "verify_ui" in SERVER_INSTRUCTIONS
    # 行数受控（宿主系统上下文注入内容应紧凑）
    assert len(SERVER_INSTRUCTIONS.splitlines()) <= 10

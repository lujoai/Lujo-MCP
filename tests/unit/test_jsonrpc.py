"""单元测试：JSON-RPC 协议"""
import asyncio
import pytest
from app.mcp.protocol.jsonrpc import (
    parse_request,
    make_response,
    make_error,
    JSONRPCRequest,
    METHOD_NOT_FOUND,
)


class TestJSONRPC:

    def test_parse_valid_request(self):
        raw = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
        req = parse_request(raw)
        assert req.jsonrpc == "2.0"
        assert req.id == 1
        assert req.method == "ping"

    def test_parse_string_id(self):
        """JSON-RPC 2.0 允许 id 为字符串"""
        raw = '{"jsonrpc":"2.0","id":"abc-123","method":"ping","params":{}}'
        req = parse_request(raw)
        assert req.id == "abc-123"
        assert isinstance(req.id, str)

    def test_parse_numeric_string_id(self):
        """数字字符串 id 不应被强转为 int"""
        raw = '{"jsonrpc":"2.0","id":"123","method":"ping","params":{}}'
        req = parse_request(raw)
        assert req.id == "123"
        assert isinstance(req.id, str)

    def test_parse_null_id(self):
        """通知消息 id 为 null"""
        raw = '{"jsonrpc":"2.0","id":null,"method":"ping","params":{}}'
        req = parse_request(raw)
        assert req.id is None

    def test_parse_invalid_json(self):
        with pytest.raises(ValueError):
            parse_request("{invalid}")

    def test_parse_missing_method(self):
        with pytest.raises(ValueError, match="method"):
            parse_request('{"jsonrpc":"2.0","id":1}')

    def test_make_response(self):
        resp = make_response(1, {"status": "ok"})
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert resp["result"] == {"status": "ok"}

    def test_make_response_string_id(self):
        resp = make_response("abc-123", {"status": "ok"})
        assert resp["id"] == "abc-123"
        assert isinstance(resp["id"], str)

    def test_make_error(self):
        resp = make_error(None, METHOD_NOT_FOUND, "Method not found")
        assert resp["jsonrpc"] == "2.0"
        assert resp["error"]["code"] == METHOD_NOT_FOUND
        assert "Method not found" in resp["error"]["message"]


class TestMCPServerDispatch:

    def test_dispatch_initialize(self):
        import asyncio
        from app.mcp.protocol.server import dispatch
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={})
        resp = asyncio.run(dispatch(req))
        assert resp["id"] == 1
        assert "protocolVersion" in resp["result"]
        assert "capabilities" in resp["result"]

    def test_dispatch_unknown_method(self):
        import asyncio
        from app.mcp.protocol.server import dispatch
        req = JSONRPCRequest(jsonrpc="2.0", id=1, method="nonexistent", params={})
        resp = asyncio.run(dispatch(req))
        assert "error" in resp
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    def test_dispatch_ping(self):
        import asyncio
        from app.mcp.protocol.server import dispatch
        req = JSONRPCRequest(jsonrpc="2.0", id=0, method="ping")
        resp = asyncio.run(dispatch(req))
        assert resp["id"] == 0
        assert "error" not in resp

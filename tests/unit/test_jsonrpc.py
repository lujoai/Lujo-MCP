"""单元测试：JSON-RPC 协议"""
import asyncio
import threading
import pytest
from app.mcp.protocol.jsonrpc import (
    parse_request,
    make_response,
    make_error,
    JSONRPCRequest,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    INVALID_REQUEST,
    INVALID_PARAMS,
    JSONParseError,
    InvalidRequestError,
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

    def test_parse_invalid_json_raises_json_parse_error(self):
        """非法 JSON → JSONParseError → -32700"""
        with pytest.raises(JSONParseError):
            parse_request("{invalid json}")

    def test_parse_non_object_raises_invalid_request_error(self):
        """非对象 JSON → InvalidRequestError → -32600"""
        with pytest.raises(InvalidRequestError, match="必须是 JSON 对象"):
            parse_request('[1, 2, 3]')

    def test_parse_missing_method_raises_invalid_request_error(self):
        """缺 method 字段 → InvalidRequestError → -32600"""
        with pytest.raises(InvalidRequestError, match="method"):
            parse_request('{"jsonrpc":"2.0","id":1}')


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

    def test_dispatch_tools_call_hides_internal_exception(self):
        from app.mcp.protocol.server import dispatch, register_tool, _tool_registry

        def _boom(arguments):
            raise RuntimeError("token=secret-value")

        original_registry = dict(_tool_registry)
        try:
            register_tool("boom", "boom", _boom, inputSchema={})
            req = JSONRPCRequest(
                jsonrpc="2.0",
                id=2,
                method="tools/call",
                params={"name": "boom", "arguments": {}},
            )
            resp = asyncio.run(dispatch(req))
            assert resp["result"]["isError"] is True
            assert resp["result"]["content"][0]["text"] == "工具执行失败，详情见服务端日志"
            assert "secret-value" not in resp["result"]["content"][0]["text"]
        finally:
            _tool_registry.clear()
            _tool_registry.update(original_registry)

    def test_dispatch_tools_call_runs_sync_handler_in_worker_thread(self):
        from app.mcp.protocol.server import dispatch, register_tool, _tool_registry

        main_thread_id = threading.get_ident()
        called_thread_ids = []

        def _sync_tool(arguments):
            called_thread_ids.append(threading.get_ident())
            return {"ok": True, "value": arguments["value"]}

        original_registry = dict(_tool_registry)
        try:
            register_tool("sync-tool", "sync", _sync_tool, inputSchema={})
            req = JSONRPCRequest(
                jsonrpc="2.0",
                id=3,
                method="tools/call",
                params={"name": "sync-tool", "arguments": {"value": 7}},
            )
            resp = asyncio.run(dispatch(req))
            assert resp["result"]["isError"] is False
            assert called_thread_ids
            assert called_thread_ids[0] != main_thread_id
        finally:
            _tool_registry.clear()
            _tool_registry.update(original_registry)


class TestDispatchRawErrorCodes:
    """验证 dispatch_raw 返回正确的 JSON-RPC 标准错误码"""

    def test_dispatch_raw_parse_error_returns_32700(self):
        """非法 JSON → -32700 Parse Error"""
        from app.mcp.protocol.server import dispatch_raw
        resp = asyncio.run(dispatch_raw("{invalid"))
        assert "error" in resp
        assert resp["error"]["code"] == PARSE_ERROR

    def test_dispatch_raw_invalid_request_returns_32600(self):
        """非对象 JSON → -32600 Invalid Request"""
        from app.mcp.protocol.server import dispatch_raw
        resp = asyncio.run(dispatch_raw("[1, 2]"))
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_REQUEST

    def test_dispatch_raw_method_not_found_returns_32601(self):
        """未知方法 → -32601 Method Not Found"""
        from app.mcp.protocol.server import dispatch_raw
        resp = asyncio.run(dispatch_raw('{"jsonrpc":"2.0","id":1,"method":"unknown"}'))
        assert "error" in resp
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    def test_parse_request_invalid_utf8_bytes_raises_unicode_decode_error(self):
        """FIX: P1-9h 畸形字节（非法 UTF-8，等价孤立代理项）抛 UnicodeDecodeError。

        stdio 传输层 except 已捕获该异常并返回 PARSE_ERROR，测试保证解析层
        以可识别异常暴露，而非让畸形输入逃逸杀进程。
        """
        with pytest.raises(UnicodeDecodeError):
            parse_request(b"\xff\xfe\x80 invalid utf-8")

    def test_dispatch_tools_call_non_dict_params_returns_32602(self):
        """FIX: P1-9i params 非 dict（list/str/null）→ -32602 Invalid Params。

        此前 params.get 直接 AttributeError → 500；解析层必须显式校验。
        """
        from app.mcp.protocol.server import dispatch

        for bad_params in (["x"], "abc", None, 42):
            # model_construct 跳过 Pydantic 校验（与 parse_request 一致），
            # 模拟从线上 JSON 解析出的畸形 params
            req = JSONRPCRequest.model_construct(
                jsonrpc="2.0",
                id=1,
                method="tools/call",
                params=bad_params,
            )
            resp = asyncio.run(dispatch(req))
            assert "error" in resp
            assert resp["error"]["code"] == INVALID_PARAMS, f"params={bad_params!r}"


class TestProtocolVersionNegotiation:
    """M5 版本协商：initialize 握手协议版本协商逻辑"""

    def test_known_version_is_echoed(self):
        """客户端请求已知版本 → 回显该版本"""
        from app.mcp.protocol.server import dispatch
        req = JSONRPCRequest(
            jsonrpc="2.0", id=1, method="initialize",
            params={"protocolVersion": "2024-08-27"},
        )
        resp = asyncio.run(dispatch(req))
        assert resp["result"]["protocolVersion"] == "2024-08-27"

    def test_latest_known_version_is_echoed(self):
        """客户端请求最新版本 → 回显该版本"""
        from app.mcp.protocol.server import dispatch
        req = JSONRPCRequest(
            jsonrpc="2.0", id=1, method="initialize",
            params={"protocolVersion": "2024-11-05"},
        )
        resp = asyncio.run(dispatch(req))
        assert resp["result"]["protocolVersion"] == "2024-11-05"

    def test_unknown_version_falls_back_with_warning(self, caplog):
        """客户端请求未知版本 → 回退到 PROTOCOL_VERSION + 记录 warning"""
        import logging
        from app.mcp.protocol.server import dispatch, PROTOCOL_VERSION
        req = JSONRPCRequest(
            jsonrpc="2.0", id=1, method="initialize",
            params={"protocolVersion": "2099-01-01"},
        )
        with caplog.at_level(logging.WARNING, logger="lujo-mcp.protocol"):
            resp = asyncio.run(dispatch(req))
        assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert "2099-01-01" in caplog.text

    def test_missing_version_falls_back_with_warning(self, caplog):
        """客户端未提供 protocolVersion → 回退 + 记录 warning"""
        import logging
        from app.mcp.protocol.server import dispatch, PROTOCOL_VERSION
        req = JSONRPCRequest(
            jsonrpc="2.0", id=1, method="initialize", params={},
        )
        with caplog.at_level(logging.WARNING, logger="lujo-mcp.protocol"):
            resp = asyncio.run(dispatch(req))
        assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert "protocolVersion" in caplog.text

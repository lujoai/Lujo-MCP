"""P0-A 工具契约：ingest 系列 MCP handler 的必填参数校验。

背景（2026-09-27 dogfooding 实证）：四个 ingest handler 对必填语义字段做
静默默认（exc_type→"UnknownError"、message→""、record→{}），宿主传错参数名时
会存入空记录并返回成功——静默假成功。新契约：必填缺失/为空时抛
ToolExecutionError（协议层转 isError=true），错误消息列出正确参数名与示例。
"""
import pytest

from app.mcp.protocol.tool_errors import ToolExecutionError
from app.mcp.tools.console_api import ingest_console_handler
from app.mcp.tools.ingest_api import ingest_error_handler
from app.mcp.tools.network_api import ingest_network_handler
from app.mcp.tools.silent_failure_api import silent_failure_handler


class TestIngestErrorContract:
    def test_missing_exc_type_rejected(self):
        with pytest.raises(ToolExecutionError) as e:
            ingest_error_handler({"message": "boom"})
        assert "exc_type" in str(e.value)

    def test_missing_message_rejected(self):
        with pytest.raises(ToolExecutionError) as e:
            ingest_error_handler({"exc_type": "TypeError"})
        assert "message" in str(e.value)

    def test_empty_strings_rejected(self):
        with pytest.raises(ToolExecutionError):
            ingest_error_handler({"exc_type": "", "message": "x"})
        with pytest.raises(ToolExecutionError):
            ingest_error_handler({"exc_type": "TypeError", "message": "  "})

    def test_valid_arguments_pass_through(self):
        res = ingest_error_handler(
            {"exc_type": "TypeError", "message": "cannot read plateNo",
             "frames": [{"file": "a.vue", "line": 1}]}
        )
        assert res["saved"] is True
        assert res["trace_id"]


class TestIngestConsoleContract:
    def test_missing_message_rejected(self):
        with pytest.raises(ToolExecutionError) as e:
            ingest_console_handler({"level": "error"})
        assert "message" in str(e.value)

    def test_valid_minimal_call_passes(self):
        res = ingest_console_handler({"message": "Network Error"})
        assert res["saved"] is True


class TestIngestNetworkContract:
    def test_missing_record_rejected(self):
        with pytest.raises(ToolExecutionError) as e:
            ingest_network_handler({"trace_id": "t1"})
        assert "record" in str(e.value)

    def test_record_without_url_rejected(self):
        with pytest.raises(ToolExecutionError) as e:
            ingest_network_handler({"record": {"status_code": 500}})
        assert "url" in str(e.value)

    def test_valid_record_passes(self):
        res = ingest_network_handler(
            {"record": {"url": "http://localhost:8080/api/x", "status_code": 401}}
        )
        assert res["saved"] is True
        assert res["record_id"]


class TestSilentFailureContract:
    def test_missing_message_rejected(self):
        with pytest.raises(ToolExecutionError) as e:
            silent_failure_handler({"observed": "nothing"})
        assert "message" in str(e.value)

    def test_valid_call_passes(self):
        res = silent_failure_handler(
            {"message": "点击登录无响应", "observed": "无请求无跳转"}
        )
        assert res["saved"] is True
        assert res["trace_id"]

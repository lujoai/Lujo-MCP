"""M2-A: 真实 stdio MCP 错误语义与 wire contract 集成测试。

使用真实子进程与标准 JSON-RPC 传输，对 stdio wire 协议边界进行全矩阵端到端验证：
1. stdio 正常 initialize（协商 protocolVersion、capabilities、serverInfo）；
2. tools/list 协议输出与工具元数据结构；
3. 正常轻量工具调用（isError=false，返回正常业务载荷）；
4. 业务结论型工具（verify 验证未通过：matched=false，isError=false，不被误判为工具崩溃）；
5. 业务错误工具调用（resolve_stack 参数不满足业务要求：isError=true，包含明确业务说明）；
6. 参数与输入校验失败（ingest_console 缺失必填参数：isError=true，error_code="INVALID_PARAMS"）；
7. 未知工具调用（isError=true，error_code="METHOD_NOT_FOUND"）；
8. 工具执行异常（内部未捕获异常：isError=true，error_code="TOOL_INTERNAL"）；
9. 工具调用超时（执行超时：isError=true，error_code="TOOL_TIMEOUT"，_timed_out=true）；
10. 并发门控拒绝（槽位打满快速失败：isError=true，error_code="TOOL_BUSY"，_busy=true）；
11. stdout/stderr 协议通道纯净性（stdout 每一行均为合法 JSON-RPC，日志严格走 stderr，错误不泄露敏感载荷）。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest


class StdioClient:
    """标准 stdio JSON-RPC 测试客户端（管理真实子进程生命周期）。"""

    def __init__(self, extra_env: dict | None = None, script_code: str | None = None):
        env = {
            **os.environ,
            "STORAGE_BACKEND": "memory",
            "API_KEY": "",
            "PYTHONIOENCODING": "utf-8",
        }
        if extra_env:
            env.update(extra_env)

        if script_code:
            cmd = [sys.executable, "-c", script_code]
        else:
            cmd = [sys.executable, "-m", "app.mcp_server", "--no-http"]

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=os.getcwd(),
        )
        self._stdout_lines: queue.Queue = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._stdout_reader, daemon=True, name="m2a-stdout-reader"
        )
        self._reader_thread.start()

    def _stdout_reader(self):
        try:
            assert self.proc.stdout is not None
            for line in iter(self.proc.stdout.readline, b""):
                if line:
                    self._stdout_lines.put(("line", line.decode("utf-8", errors="replace")))
        except Exception as e:
            self._stdout_lines.put(("error", e))

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def recv(self, timeout: float = 10.0) -> dict:
        try:
            kind, val = self._stdout_lines.get(timeout=timeout)
            if kind == "error":
                raise val
            return json.loads(val.strip())
        except queue.Empty:
            stderr = self.read_stderr_safe()
            raise TimeoutError(f"等待 stdout JSON-RPC 响应超时 ({timeout}s)。stderr:\n{stderr}")

    def initialize(self) -> dict:
        self.send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "m2a-test-client", "version": "1.0"},
            },
        })
        resp = self.recv(timeout=15.0)
        self.send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
        return resp

    def close(self) -> tuple[int, str]:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            returncode = self.proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            returncode = self.proc.wait()
        stderr_text = self.read_stderr_safe()
        return returncode, stderr_text

    def read_stderr_safe(self) -> str:
        if self.proc.stderr is None:
            return ""
        try:
            return self.proc.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            return ""


# ── 一、握手、列表与标准成功/业务结论契约 ────────────────────────────────


class TestStdioStandardWireContract:
    def test_initialize_and_tools_list_wire_structure(self):
        """1 & 2: 真实 stdio 握手与 tools/list 清单格式契约。"""
        client = StdioClient()
        try:
            init_resp = client.initialize()
            assert init_resp["jsonrpc"] == "2.0"
            assert init_resp["id"] == 1
            result = init_resp["result"]
            assert result["protocolVersion"] == "2024-11-05"
            assert result["serverInfo"]["name"] == "lujo-mcp"
            assert "tools" in result["capabilities"]

            client.send({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            })
            list_resp = client.recv()
            assert list_resp["jsonrpc"] == "2.0"
            assert list_resp["id"] == 2
            tools = list_resp["result"]["tools"]
            assert len(tools) > 0
            tool_names = [t["name"] for t in tools]
            assert "list_recent_traces" in tool_names
            assert "verify" in tool_names
            assert "resolve_stack" in tool_names
        finally:
            code, _ = client.close()
            assert code == 0

    def test_normal_lightweight_tool_call_success(self):
        """3: 正常轻量工具调用返回 isError=false 及预期 JSON 载荷结构。"""
        client = StdioClient()
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_recent_traces",
                    "arguments": {"limit": 2},
                },
            })
            resp = client.recv()
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 3
            assert resp["result"]["isError"] is False
            content = resp["result"]["content"]
            assert len(content) == 1
            assert content[0]["type"] == "text"
            payload = json.loads(content[0]["text"])
            assert "count" in payload
            assert "traces" in payload
        finally:
            code, _ = client.close()
            assert code == 0

    def test_conclusion_tool_failure_is_not_mcp_error(self):
        """4: 结论型工具验证失败（matched=false）是业务结论，isError 必须为 false。"""
        client = StdioClient()
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "verify",
                    "arguments": {
                        "actual": {"status_code": 500, "body": {"err": "internal"}},
                        "spec": {
                            "kind": "api",
                            "target": "POST /api/action",
                            "expect": {"status": 200},
                        },
                    },
                },
            })
            resp = client.recv()
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 4
            # 关键契约：验证不通过是正常业务结论，绝不得标为 isError=true
            assert resp["result"]["isError"] is False
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert payload["matched"] is False
            assert "diffs" in payload
            assert payload["silent_failure"] is False
        finally:
            code, _ = client.close()
            assert code == 0

    def test_business_failure_is_marked_as_is_error(self):
        """5: 业务层处理失败（如 resolve_stack 传空帧）必须明确标记 isError=true。"""
        client = StdioClient()
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "resolve_stack",
                    "arguments": {"frames": []},
                },
            })
            resp = client.recv()
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 5
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert "error" in payload
            assert "frames" in payload["error"]
        finally:
            code, _ = client.close()
            assert code == 0


# ── 二、参数错误与未知工具错误契约 ──────────────────────────────────────


class TestStdioValidationAndUnknownToolErrors:
    def test_missing_required_arguments_returns_invalid_params_error_code(self):
        """6: 缺失必填参数时，isError=true 且携带稳定的 error_code='INVALID_PARAMS'。"""
        client = StdioClient()
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "ingest_console",
                    "arguments": {},
                },
            })
            resp = client.recv()
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 6
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert payload["error_code"] == "INVALID_PARAMS"
            assert "message" in payload["error"]
        finally:
            code, _ = client.close()
            assert code == 0

    def test_unknown_tool_returns_method_not_found_error_code(self):
        """7: 调用未注册的未知工具时，isError=true 且携带 error_code='METHOD_NOT_FOUND'。"""
        client = StdioClient()
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "unknown_tool_foo_bar",
                    "arguments": {},
                },
            })
            resp = client.recv()
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 7
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert "未知工具" in payload["error"]
            assert payload.get("error_code") == "METHOD_NOT_FOUND"
        finally:
            code, _ = client.close()
            assert code == 0


# ── 三、内部异常、超时与并发门控拒绝契约 ────────────────────────────────


class TestStdioExecutionLifecycleAndConcurrencyErrors:
    def test_internal_exception_returns_tool_internal_error_code(self):
        """8: 工具内部未捕获异常，isError=true 且携带 error_code='TOOL_INTERNAL'。"""
        code = """
import asyncio
from app.mcp.protocol.server import register_tool
from app.mcp_server import main

def boom_handler(args):
    raise RuntimeError("unhandled internal boom")

register_tool("wire_test_boom", description="boom", handler=boom_handler, inputSchema={"type": "object"})
asyncio.run(main(["--no-http"]))
"""
        client = StdioClient(script_code=code)
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "wire_test_boom", "arguments": {}},
            })
            resp = client.recv()
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 8
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert "Tool execution failed" in payload["error"]
            assert payload.get("error_code") == "TOOL_INTERNAL"
        finally:
            code, _ = client.close()
            assert code == 0

    def test_tool_timeout_returns_tool_timeout_error_code(self):
        """9: 工具调用超时，isError=true 且携带 error_code='TOOL_TIMEOUT' 及 _timed_out=true。"""
        code = """
import asyncio, time
from app.mcp.protocol.server import register_tool
from app.mcp_server import main

def slow_handler(args):
    time.sleep(3.0)
    return {"ok": True}

register_tool("wire_test_slow", description="slow", handler=slow_handler, inputSchema={"type": "object"})
asyncio.run(main(["--no-http"]))
"""
        client = StdioClient(
            extra_env={"TOOL_TIMEOUT_SECONDS": "1"},
            script_code=code,
        )
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "wire_test_slow", "arguments": {}},
            })
            resp = client.recv()
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 9
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert payload.get("_timed_out") is True
            assert payload.get("error_code") == "TOOL_TIMEOUT"
        finally:
            code, _ = client.close()
            assert code == 0

    def test_concurrency_busy_fast_fail_returns_tool_busy(self):
        """10: 槽位打满时 fast-fail，isError=true 且携带 error_code='TOOL_BUSY' 及 _busy=true。"""
        code = """
import asyncio, time
from app.mcp.protocol.server import register_tool
from app.mcp_server import main

def occupier_handler(args):
    time.sleep(2.0)
    return {"ok": True}

register_tool("wire_occupier", description="occupier", handler=occupier_handler, inputSchema={"type": "object"})
asyncio.run(main(["--no-http"]))
"""
        client = StdioClient(
            extra_env={
                "TOOL_EXECUTOR_WORKERS": "1",
                "TOOL_BUSY_QUEUE_TIMEOUT": "0.0",
            },
            script_code=code,
        )
        try:
            client.initialize()
            # 发送第一个请求占用唯一槽位
            client.send({
                "jsonrpc": "2.0",
                "id": 100,
                "method": "tools/call",
                "params": {"name": "wire_occupier", "arguments": {}},
            })
            # 立即发送第二个请求，触发快速拒绝
            client.send({
                "jsonrpc": "2.0",
                "id": 101,
                "method": "tools/call",
                "params": {"name": "wire_occupier", "arguments": {}},
            })
            resp101 = client.recv()
            assert resp101["jsonrpc"] == "2.0"
            assert resp101["id"] == 101
            assert resp101["result"]["isError"] is True
            payload = json.loads(resp101["result"]["content"][0]["text"])
            assert payload.get("error_code") == "TOOL_BUSY"
            assert payload.get("_busy") is True

            # 接收第一个请求完成的响应
            resp100 = client.recv(timeout=10.0)
            assert resp100["id"] == 100
        finally:
            code, _ = client.close()
            assert code == 0


# ── 四、协议纯净性与敏感信息隔离 ────────────────────────────────────────


class TestStdioProtocolPurityAndSecuritySanitization:
    def test_stdout_is_pure_jsonrpc_and_stderr_is_logging_only(self):
        """11.1: 确认 stdout 每一行均为合法 JSON-RPC，stderr 接收日志，两者无交叉污染。"""
        client = StdioClient()
        try:
            client.initialize()
            for req_id in range(10, 15):
                client.send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {
                        "name": "list_recent_traces",
                        "arguments": {"limit": 1},
                    },
                })
                resp = client.recv()
                assert resp["id"] == req_id
                assert resp["jsonrpc"] == "2.0"
        finally:
            code, stderr_text = client.close()
            assert code == 0

        # stderr 包含标准日志格式，但不含 stdout 响应帧
        assert len(stderr_text) > 0
        assert "INFO" in stderr_text or "WARNING" in stderr_text
        assert '{"jsonrpc": "2.0"' not in stderr_text

    def test_error_response_does_not_leak_secrets_or_sensitive_paths(self):
        """11.2: 错误响应中绝不泄露 API key、真实密码或非预期系统路径。"""
        client = StdioClient()
        try:
            client.initialize()
            client.send({
                "jsonrpc": "2.0",
                "id": 88,
                "method": "tools/call",
                "params": {
                    "name": "ingest_console",
                    "arguments": {
                        "api_key": "sk-super-secret-token-do-not-leak",
                        "secret_path": "C:\\SecretDir\\confidential.key",
                        "invalid_type": 12345,
                    },
                },
            })
            resp = client.recv()
            assert resp["result"]["isError"] is True
            raw_text = resp["result"]["content"][0]["text"]
            assert "sk-super-secret-token-do-not-leak" not in raw_text
            assert "confidential.key" not in raw_text
        finally:
            code, _ = client.close()
            assert code == 0

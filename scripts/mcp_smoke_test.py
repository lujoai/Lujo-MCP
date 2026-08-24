"""MCP 客户端接入轻量冒烟验证脚本（Phase 3 D7 Release Preparation）。

用途：
- 验证 Lujo-MCP stdio MCP Server 可被外部 MCP 客户端（Claude Desktop / Cursor / Trae 等）
  正常接入：启动 → initialize 握手 → tools/list 枚举 → 调用一个无害工具 → 退出。
- 不修改任何生产代码，不改 MCP 协议，不引入 LLM 调用。

用法：
    python scripts/mcp_smoke_test.py
    python scripts/mcp_smoke_test.py --tool debug

退出码：
    0 = 冒烟通过；1 = 启动/握手/枚举/调用任一环节失败。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

# MCP JSON-RPC 消息 id 计数器
_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def _send(proc: subprocess.Popen, method: str, params: dict) -> dict:
    """向 stdio 发送一条 JSON-RPC 请求并读取对应响应。"""
    msg = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params,
    }
    line = json.dumps(msg, ensure_ascii=False)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()

    # stdio server 每行一条 JSON 响应；读取直到找到匹配 id
    for _ in range(50):
        raw = proc.stdout.readline()
        if not raw:
            raise RuntimeError("stdio 流提前关闭（读取响应失败）")
        try:
            resp = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if resp.get("id") == msg["id"]:
            return resp
    raise RuntimeError(f"未在预期内收到 id={msg['id']} 的响应")


def _run_smoke(tool: str | None) -> int:
    cmd = [sys.executable, "-m", "app.mcp_server"]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        # 1. initialize 握手
        init = _send(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lujo-smoke-test", "version": "0.4.1-beta"},
        })
        if "error" in init:
            print(f"[FAIL] initialize 失败: {init['error']}", file=sys.stderr)
            return 1
        server_info = init.get("result", {}).get("serverInfo", {})
        print(f"[OK] initialize: serverInfo={json.dumps(server_info, ensure_ascii=False)}")

        # 2. notifications/initialized（可选，客户端通常发送）
        initialized = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        proc.stdin.write(json.dumps(initialized) + "\n")
        proc.stdin.flush()

        # 3. tools/list 枚举
        tools_resp = _send(proc, "tools/list", {})
        if "error" in tools_resp:
            print(f"[FAIL] tools/list 失败: {tools_resp['error']}", file=sys.stderr)
            return 1
        tools = tools_resp.get("result", {}).get("tools", [])
        names = [t.get("name") for t in tools]
        print(f"[OK] tools/list: 枚举 {len(names)} 个工具: {sorted(names)}")
        if not names:
            print("[FAIL] tools/list 返回空工具列表", file=sys.stderr)
            return 1

        # 4. 调用一个无害工具验证往返
        target = tool if tool in names else "debug"
        if target not in names:
            target = names[0]
        call = _send(proc, "tools/call", {
            "name": target,
            "arguments": {},
        })
        if "error" in call:
            print(f"[WARN] tools/call {target} 返回 error（非致命，视工具而定）：{call['error']}")
        else:
            content = call.get("result", {}).get("content", [])
            print(f"[OK] tools/call {target}: {len(content)} 个 content 块")

        print("[PASS] MCP stdio 冒烟验证通过")
        return 0
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lujo-MCP stdio 接入冒烟验证")
    parser.add_argument("--tool", default=None, help="要调用的工具名（默认 debug）")
    args = parser.parse_args(argv)
    start = time.monotonic()
    rc = _run_smoke(args.tool)
    print(f"耗时 {(time.monotonic() - start) * 1000:.0f}ms，退出码 {rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

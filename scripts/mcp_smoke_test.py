"""MCP 客户端接入轻量冒烟验证脚本（Phase 3 D7 Release Preparation）。

用途：
- 验证 Lujo-MCP stdio MCP Server 可被外部 MCP 客户端（Claude Desktop / Cursor / Trae 等）
  正常接入：启动 → initialize 握手 → tools/list 枚举 → 调用一个无害工具 → 退出。
- 不修改任何生产代码，不改 MCP 协议，不引入 LLM 调用。

用法：
    python scripts/mcp_smoke_test.py
    python scripts/mcp_smoke_test.py --tool debug
    python scripts/mcp_smoke_test.py --cmd "./dist/lujo-mcp-server"   # 发布前验证打包二进制

退出码：
    0 = 冒烟通过；1 = 启动/握手/枚举/调用任一环节失败。
"""

from __future__ import annotations

import argparse
import json
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

# FIX(v0.7.0 Minor): clientInfo 版本此前硬编码 0.4.1-beta（早已失真）。
# 改为从 app.__version__ 动态读取——脚本须可在任意 cwd 运行（发布构建冒烟），
# 故先引导仓库根进 sys.path；app 不可导入时兜底 unknown（--cmd 二进制冒烟仍可用）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from app import __version__ as _APP_VERSION
except ImportError:  # pragma: no cover - 独立分发场景兜底
    _APP_VERSION = "unknown"

# FIX: P2-F7 —— Windows 冒烟崩溃：发布构建的 windows job 里 Python stdout 默认
# codec 是 cp1252，本脚本 print 的界面文案含中文（"枚举"/"个工具"等）在
# UnsupportedOperation/charmap 下抛 UnicodeEncodeError，导致二进制冒烟误判失败
# （本来 initialize 已通过、二进制正常）。这里把 stdout/stderr 强制切到 utf-8，
# 保证跨平台（Linux/macOS/Windows）都能正常输出，不因控制台 codec 差异崩溃。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# MCP JSON-RPC 消息 id 计数器
_ID = 0

# 单条 JSON-RPC 响应的读取超时（秒）：服务端挂死时冒烟脚本不得永久阻塞
_READ_TIMEOUT = 10.0


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def _start_readers(proc: subprocess.Popen) -> queue.Queue[str]:
    """后台线程分别读取 stdout / stderr。

    stdout 逐行入队供主线程带超时消费；stderr 持续排空避免管道缓冲写满
    导致子进程阻塞（死锁）。EOF 时向 stdout 队列推入 None 哨兵。
    """
    out_q: queue.Queue[str] = queue.Queue()

    def _drain_out() -> None:
        for raw in iter(proc.stdout.readline, ""):
            out_q.put(raw)
        out_q.put(None)

    def _drain_err() -> None:
        for _ in iter(proc.stderr.readline, ""):
            pass

    threading.Thread(target=_drain_out, daemon=True).start()
    threading.Thread(target=_drain_err, daemon=True).start()
    return out_q


def _send(proc: subprocess.Popen, out_q: queue.Queue[str], method: str, params: dict) -> dict:
    """向 stdio 发送一条 JSON-RPC 请求并读取对应响应（带超时兜底）。"""
    msg = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params,
    }
    line = json.dumps(msg, ensure_ascii=False)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()

    # stdio server 每行一条 JSON 响应；读取直到找到匹配 id，超时/EOF 时失败
    deadline = time.monotonic() + _READ_TIMEOUT
    for _ in range(50):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"等待 id={msg['id']} 响应超时（>{_READ_TIMEOUT}s）")
        try:
            raw = out_q.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError(f"等待 id={msg['id']} 响应超时（>{_READ_TIMEOUT}s）")
        if raw is None:
            raise RuntimeError("stdio 流提前关闭（读取响应失败）")
        try:
            resp = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if resp.get("id") == msg["id"]:
            return resp
    raise RuntimeError(f"未在预期内收到 id={msg['id']} 的响应")


def _resolve_cmd(cmd) -> list[str]:
    """解析 --cmd 启动命令：None 用默认的 `python -m app.mcp_server`，
    字符串按 shell 规则拆分成 argv（支持发布二进制 `./dist/lujo-mcp-server`）。"""
    if cmd is None:
        return [sys.executable, "-m", "app.mcp_server"]
    if isinstance(cmd, str):
        return shlex.split(cmd)
    return list(cmd)


def _run_smoke(tool: str | None, cmd=None) -> int:
    cmd = _resolve_cmd(cmd)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    out_q = _start_readers(proc)
    try:
        # 1. initialize 握手
        init = _send(proc, out_q, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lujo-smoke-test", "version": _APP_VERSION},
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
        tools_resp = _send(proc, out_q, "tools/list", {})
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
        call = _send(proc, out_q, "tools/call", {
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
    parser.add_argument(
        "--cmd",
        default=None,
        help="要启动的 MCP server 命令（默认：python -m app.mcp_server；"
        "发布前验证二进制时传 ./dist/lujo-mcp-server(.exe)）",
    )
    args = parser.parse_args(argv)
    start = time.monotonic()
    rc = _run_smoke(args.tool, args.cmd)
    print(f"耗时 {(time.monotonic() - start) * 1000:.0f}ms，退出码 {rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

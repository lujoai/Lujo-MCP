"""MCP 客户端接入轻量冒烟验证脚本（Phase 3 D7 Release Preparation）。

用途：
- 验证 Lujo-MCP stdio MCP Server 可被外部 MCP 客户端（Claude Desktop / Cursor / Trae 等）
  正常接入：启动 → initialize 握手 → tools/list 枚举 → 调用一个无害工具 → 退出。
- 不修改任何生产代码，不改 MCP 协议，不引入 LLM 调用。
- 传入 `--http-url` / `--http-probe-urls` 时还会等待统一模式的 HTTP 接口（如 /health 与 /demo），与 stdio 握手在同一服务进程存活期内一起验证。

用法：
    python scripts/mcp_smoke_test.py
    python scripts/mcp_smoke_test.py --tool debug
    python scripts/mcp_smoke_test.py --cmd "./dist/lujo-mcp-server"   # 发布前验证打包二进制

退出码：
    0 = 冒烟通过；1 = 启动/握手/枚举/调用任一环节失败。
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import queue
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
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
_DEFAULT_READ_TIMEOUT = 10.0
_READ_TIMEOUT = _DEFAULT_READ_TIMEOUT
# 发布构建可通过 --read-timeout 放宽冷启动较慢的冻结进程（尤其 Windows
# heavy 子进程）验证时间；默认值保持轻量开发冒烟的快速失败语义。

# HTTP health readiness 超时（秒）：独立于 stdio 响应超时，因为 PyInstaller
# 冻结二进制 --http 模式冷启动需要解压归档 + 导入 FastAPI/uvicorn，在 CI
# runner 的慢磁盘上可能超过 10 秒。默认 15 秒给 Windows runner 足够余量。
_DEFAULT_HTTP_TIMEOUT = 15.0


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
    stderr_tail: deque[str] = deque(maxlen=80)
    stderr_lock = threading.Lock()

    def _drain_out() -> None:
        for raw in iter(proc.stdout.readline, ""):
            out_q.put(raw)
        out_q.put(None)

    def _drain_err() -> None:
        for raw in iter(proc.stderr.readline, ""):
            with stderr_lock:
                stderr_tail.append(raw.rstrip())

    threading.Thread(target=_drain_out, daemon=True).start()
    stderr_thread = threading.Thread(target=_drain_err, daemon=True)
    stderr_thread.start()
    # Keep the helper's queue-only return contract for existing callers/test doubles.
    out_q.stderr_tail = stderr_tail
    out_q.stderr_lock = stderr_lock
    out_q.stderr_thread = stderr_thread
    return out_q


def _print_stderr_tail(stderr_tail: deque[str], stderr_lock: threading.Lock) -> None:
    """Print a bounded server log tail for a failed tool call, never on success."""
    with stderr_lock:
        lines = list(stderr_tail)
    if not lines:
        return
    print(
        f"[DEBUG] 服务端 stderr（最近 {len(lines)} 行）:",
        file=sys.stderr,
    )
    for line in lines:
        print(line, file=sys.stderr)


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


def _parse_tool_arguments(raw: str) -> dict:
    """解析冒烟工具参数，拒绝数组/标量以保持 tools/call 契约。"""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--arguments-json 不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("--arguments-json 必须是 JSON 对象")
    return value


# 网络捕获演示页（/demo）独有、稳定的标识内容特征。
# 必须包含其中至少一个特征，防止将任意 2xx 或仅含通用 "demo" 的无关 HTML 误判为就绪。
_DEMO_SIGNATURE_MARKERS: tuple[str, ...] = (
    "AI Debug Network Capture Demo",
    "ingestion-status",
    "testXhrGet",
)


def _validate_http_response(url: str, status: int, content_type: str, raw_text: str) -> dict:
    """校验 HTTP 探测端点响应有效性。

    校验规则：
    - 状态码必须在 2xx 范围内（200 <= status < 300）；
    - /health：必须返回 2xx、必须是合法 JSON 对象，且 status 字段必须为预期合法状态（"ok" 或 "degraded"），拒绝非 JSON 或异常状态；
    - /demo：Content-Type 必须包含 text/html，且响应文本必须包含网络捕获演示页独有、稳定的标识内容
      （如 "AI Debug Network Capture Demo"、"ingestion-status" 或 "testXhrGet"），
      拒绝通用 "demo" 或把任意 2xx / 包含普通 "Demo" 的 HTML 页面判为通过；
    - 其他端点：优先解析为 JSON 对象，非 JSON 则返回状态码与元信息。
    """
    if not (200 <= status < 300):
        raise ValueError(f"HTTP 状态码异常 ({status})，不在 2xx 成功范围内")

    parsed_path = urllib.parse.urlparse(url).path.rstrip("/")
    if parsed_path == "/health" or parsed_path.endswith("/health"):
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"/health 响应不是合法的 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"/health 响应必须是 JSON 对象，实际为 {type(payload).__name__}")
        status_val = payload.get("status")
        if status_val not in ("ok", "degraded"):
            raise ValueError(f"/health 响应 status 异常：{status_val!r}，预期为 'ok' 或 'degraded'")
        return payload

    if parsed_path == "/demo" or parsed_path.endswith("/demo"):
        ct_lower = content_type.lower()
        if "text/html" not in ct_lower:
            raise ValueError(f"/demo 响应 Content-Type 必须包含 text/html，实际为 {content_type!r}")
        if not any(marker in raw_text for marker in _DEMO_SIGNATURE_MARKERS):
            raise ValueError(
                f"/demo 页面缺少网络捕获演示页独有标识（未包含 {', '.join(_DEMO_SIGNATURE_MARKERS)!r} 中的任一独有特征）"
            )
        return {
            "status": status,
            "content_type": content_type,
            "length": len(raw_text),
        }

    try:
        payload = json.loads(raw_text)
        return payload if isinstance(payload, dict) else {"value": payload}
    except json.JSONDecodeError:
        return {
            "status": status,
            "content_type": content_type,
            "length": len(raw_text),
        }


def _wait_http(url: str, timeout: float = _READ_TIMEOUT) -> dict:
    """等待本机 HTTP 端点（/health 或 /demo 等）就绪并校验有效性，供统一 transport 发布冒烟使用。"""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                status = getattr(response, "status", 200)
                headers = getattr(response, "headers", None)
                content_type = headers.get("Content-Type", "") if headers and hasattr(headers, "get") else ""
                raw_bytes = response.read()
                raw_text = raw_bytes.decode("utf-8") if isinstance(raw_bytes, bytes) else str(raw_bytes)
                return _validate_http_response(url, status, content_type, raw_text)
        except Exception as exc:
            last_error = exc
        time.sleep(0.05)
    raise TimeoutError(f"等待 HTTP 端点超时（>{timeout}s）: {url}; last={last_error}")


def _normalize_http_urls(
    http_url: str | list[str] | None = None,
    http_probe_urls: str | list[str] | None = None,
) -> list[str]:
    """将 --http-url / --http-probe-urls 等多种传入形式统一规范化为 URL列表。"""
    raw_candidates: list[str] = []
    for item in (http_url, http_probe_urls):
        if not item:
            continue
        if isinstance(item, str):
            raw_candidates.append(item)
        else:
            raw_candidates.extend(item)

    urls: list[str] = []
    for candidate in raw_candidates:
        for u in candidate.split(","):
            cleaned = u.strip()
            if cleaned and cleaned not in urls:
                urls.append(cleaned)
    return urls


def _cleanup_process(proc: subprocess.Popen) -> None:
    """清理子进程（stdio 关闭与阶梯收口）。

    收口流程：
    1. 优先关闭 stdin 触发服务进程优雅退出（stdio_eof 路径）；
    2. 等待进程退出，若超时则阶梯式调用 terminate() 与 kill()。

    进程树清理与孤儿进程现实约束评估（Node launcher → native binary）：
    - 正常路径下，通过关闭 stdin 触发 stdio EOF，由 Node launcher 及底层二进制自退；
    - 当通过 npm 启动器（node cli.js）启动服务时，Node 为直接子进程，本地二进制为孙进程；
    - Windows 下跨进程树（Node -> Binary）级联清理受操作系统机制限制（Job Object 或 taskkill /T
      在 Python 标准库 Popen.terminate/kill 中并不默认跨层级递归），无法 100% 保证树级无孤儿；
    - 因此本清理属于对直接子进程的最佳努力（best-effort）收口，明确如实表述为 best-effort，
      绝不宣称“彻底杜绝孤儿进程”。
    """
    if proc.stdin and not proc.stdin.closed:
        try:
            proc.stdin.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        pass


def _run_smoke(
    tool: str | None,
    cmd=None,
    http_url: str | list[str] | None = None,
    tool_arguments: dict | None = None,
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT,
    http_probe_urls: str | list[str] | None = None,
) -> int:
    cmd = _resolve_cmd(cmd)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    out_q = _start_readers(proc)
    stderr_tail = getattr(out_q, "stderr_tail", deque(maxlen=80))
    stderr_lock = getattr(out_q, "stderr_lock", threading.Lock())
    stderr_thread = getattr(out_q, "stderr_thread", None)
    try:
        probe_urls = _normalize_http_urls(http_url, http_probe_urls)
        if probe_urls:
            for p_url in probe_urls:
                try:
                    res = _wait_http(p_url, timeout=http_timeout)
                    if "/health" in p_url:
                        print(f"[OK] HTTP health: {json.dumps(res, ensure_ascii=False)}")
                    elif "/demo" in p_url:
                        print(f"[OK] HTTP demo ({p_url}): {json.dumps(res, ensure_ascii=False)}")
                    else:
                        print(f"[OK] HTTP probe ({p_url}): {json.dumps(res, ensure_ascii=False)}")
                except Exception as exc:
                    print(f"[FAIL] HTTP 探测失败 ({p_url}): {exc}", file=sys.stderr)
                    return 1

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
        if tool is not None:
            if tool not in names:
                print(f"[FAIL] 指定的工具 {tool} 不在 tools/list 返回的工具中", file=sys.stderr)
                return 1
            target = tool
        else:
            target = "debug" if "debug" in names else names[0]
        call = _send(proc, out_q, "tools/call", {
            "name": target,
            "arguments": tool_arguments or {},
        })
        if "error" in call:
            print(f"[FAIL] tools/call {target} 返回协议 error：{call['error']}", file=sys.stderr)
            return 1
        result = call.get("result", {})
        if result.get("isError"):
            print(f"[FAIL] tools/call {target} 返回 isError=true：{result}", file=sys.stderr)
            _cleanup_process(proc)
            if stderr_thread is not None:
                stderr_thread.join(timeout=2)
            _print_stderr_tail(stderr_tail, stderr_lock)
            return 1
        content = result.get("content", [])
        print(f"[OK] tools/call {target}: {len(content)} 个 content 块")

        print("[PASS] MCP stdio 冒烟验证通过")
        return 0
    finally:
        _cleanup_process(proc)


def main(argv: list[str] | None = None) -> int:
    global _READ_TIMEOUT
    parser = argparse.ArgumentParser(description="Lujo-MCP stdio 接入冒烟验证")
    parser.add_argument("--tool", default=None, help="要调用的工具名（默认 debug）")
    parser.add_argument(
        "--arguments-json",
        default="{}",
        help="工具调用参数 JSON 对象（用于验证需要入参的工具）",
    )
    parser.add_argument(
        "--cmd",
        default=None,
        help="要启动的 MCP server 命令（默认：python -m app.mcp_server；"
        "发布前验证二进制时传 ./dist/lujo-mcp-server(.exe)）",
    )
    parser.add_argument(
        "--http-url",
        action="append",
        default=None,
        help="可选：等待就绪的 HTTP 端点 URL（可多次传入或逗号分隔，如 /health 与 /demo）",
    )
    parser.add_argument(
        "--http-probe-urls",
        action="append",
        default=None,
        help="可选：待探测的 HTTP URL 列表（支持多次传入或逗号分隔，功能同 --http-url）",
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=_DEFAULT_READ_TIMEOUT,
        help="单条 MCP 响应读取超时（秒，默认 10；冻结构建可适当放宽）",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=_DEFAULT_HTTP_TIMEOUT,
        help="HTTP 端点就绪探测超时（秒，默认 15；CI 冻结构建可放宽至 30）",
    )
    args = parser.parse_args(argv)
    if args.read_timeout <= 0:
        parser.error("--read-timeout 必须大于 0")
    if args.http_timeout <= 0:
        parser.error("--http-timeout 必须大于 0")
    _READ_TIMEOUT = args.read_timeout
    try:
        tool_arguments = _parse_tool_arguments(args.arguments_json)
    except ValueError as exc:
        parser.error(str(exc))
    start = time.monotonic()
    rc = _run_smoke(
        args.tool,
        args.cmd,
        args.http_url,
        tool_arguments,
        args.http_timeout,
        http_probe_urls=args.http_probe_urls,
    )
    print(f"耗时 {(time.monotonic() - start) * 1000:.0f}ms，退出码 {rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

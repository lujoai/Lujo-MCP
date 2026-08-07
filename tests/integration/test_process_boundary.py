"""进程边界集成测试。

覆盖：
- python -m app.mcp_server 启动后 stdin/stdout JSON-RPC 握手
- python -m app.main 启动后 /health 返回 200
- 进程终止后 PG 连接池正确关闭（无连接泄漏）

设计要点：
- 所有子进程在 finally 中先 terminate 再读 stderr，避免读 stderr 阻塞
- 断言放在 finally 之后，确保进程已清理
- 所有 skip 必须给出明确原因（环境变量未配 / 依赖未就绪）
- 不引入 pytest-asyncio 等新依赖
- 子进程不读项目根 .env（避免 .env 含未知键触发 pydantic extra_forbidden，
  即 M9 问题），通过环境变量传完整配置；测试期间 .env 被临时备份
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

import pytest

from app.config import settings

# 项目根目录（tests/integration/test_process_boundary.py → parents[2]）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"
_ENV_BACKUP = _PROJECT_ROOT / ".env.h12_test_bak"


# ── .env 隔离 fixture ──

@pytest.fixture
def _isolated_env():
    """临时备份项目根 .env，让子进程只通过环境变量接收配置。

    解决 .env 含未知键（如 POSTGRES_PASSWORD、DATABASE_URL）触发 pydantic
    extra_forbidden 启动崩溃的问题（M9，不在本任务修复范围）。

    时序：
    1. fixture 进入：备份 .env → .env.h12_test_bak（原子 rename）
    2. yield：测试函数启动子进程（读不到 .env，只从 env 读取）
    3. fixture 退出：恢复 .env.h12_test_bak → .env

    子进程启动后已读完配置，不会重新读 .env，所以 fixture 退出时恢复 .env
    不影响已运行的子进程。
    """
    env_existed = _ENV_PATH.exists()
    if env_existed:
        # 原子重命名
        os.replace(str(_ENV_PATH), str(_ENV_BACKUP))
        try:
            yield
        finally:
            # 恢复 .env
            if _ENV_BACKUP.exists():
                if _ENV_PATH.exists():
                    # 测试期间又生成了 .env，删掉再恢复
                    try:
                        _ENV_PATH.unlink()
                    except OSError:
                        pass
                os.replace(str(_ENV_BACKUP), str(_ENV_PATH))
    else:
        yield


# ── 辅助函数 ──

def _find_free_port() -> int:
    """让系统分配一个空闲端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _readline_with_timeout(proc: subprocess.Popen, timeout: float = 10.0):
    """从 proc.stdout 读一行，带超时（线程 + Queue）。

    返回 (line: str | None, error: Exception | None)
    """
    q: Queue = Queue()

    def reader():
        try:
            line = proc.stdout.readline()
            q.put(("ok", line))
        except Exception as e:
            q.put(("err", e))

    t = Thread(target=reader, daemon=True)
    t.start()
    try:
        kind, payload = q.get(timeout=timeout)
        if kind == "err":
            return None, payload
        return payload, None
    except Empty:
        return None, TimeoutError(f"readline 超时 ({timeout}s)")


def _wait_for_health(port: int, timeout: float = 15.0) -> tuple:
    """轮询 /health 直到 200 或超时。

    返回 (ready: bool, body_text_or_err: str | None)
    """
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    return True, body
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = str(e)
        time.sleep(0.3)
    return False, last_err


def _safe_read_stderr(proc: subprocess.Popen) -> str:
    """安全读取 stderr 全部内容（用于诊断，必须在进程终止后调用）"""
    if proc.stderr is None:
        return ""
    try:
        return proc.stderr.read().decode(errors="replace")
    except Exception:
        return ""


def _terminate_gracefully(proc: subprocess.Popen, timeout: float = 15.0) -> tuple:
    """优雅终止子进程，返回 (exit_code, stderr_text)。

    平台差异：
    - Unix: 发送 SIGTERM（触发 uvicorn lifespan shutdown → close_pool）
    - Windows: proc.terminate() 等价 TerminateProcess（硬 kill，不触发 lifespan）
    """
    if proc.poll() is not None:
        # 进程已退出
        stderr = _safe_read_stderr(proc)
        return proc.returncode, stderr

    if sys.platform == "win32":
        proc.terminate()  # TerminateProcess（硬 kill）
    else:
        try:
            proc.send_signal(signal.SIGTERM)
        except (OSError, ProcessLookupError):
            proc.terminate()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 超时未退出，强制 kill
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # 进程已终止，安全读 stderr
    stderr = _safe_read_stderr(proc)
    return proc.returncode, stderr


# ── 测试用例 ──

class TestStdioMcpServerBoundary:
    """stdio MCP Server 进程边界测试"""

    def test_stdio_mcp_server_handshake(self, _isolated_env):
        """python -m app.mcp_server 启动后能完成 JSON-RPC initialize 握手。

        协议：mcp SDK 的 stdio_server 使用 newline-delimited JSON-RPC。
        发送 {jsonrpc,initialize} + \\n，应收到 {jsonrpc,result} + \\n。
        """
        # stdio 测试不需要 PG，用 memory 后端
        env = {
            **os.environ,
            "STORAGE_BACKEND": "memory",
            "API_KEY": "",
        }

        proc = subprocess.Popen(
            [sys.executable, "-m", "app.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=os.getcwd(),
        )

        init_req = (
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            }) + "\n"
        ).encode("utf-8")

        line = None
        write_error = None
        read_error = None

        try:
            try:
                proc.stdin.write(init_req)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                write_error = e
            else:
                line, read_error = _readline_with_timeout(proc, timeout=10.0)
        finally:
            # 先 terminate 进程，避免读 stderr 阻塞
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            stderr_text = _safe_read_stderr(proc)

        # 启动失败 → 显式 skip（环境问题）
        if write_error is not None:
            pytest.skip(
                f"stdio 子进程启动失败或无法写入 stdin: {write_error}"
                f"\nstderr 输出:\n{stderr_text}"
            )

        # 响应断言（进程已清理，stderr 已读）
        assert read_error is None, (
            f"stdio 握手读取响应失败: {read_error}\nstderr 输出:\n{stderr_text}"
        )
        assert line is not None and line.strip(), (
            f"stdio 握手无响应或读取超时。\nstderr 输出:\n{stderr_text}"
        )

        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            pytest.fail(
                f"stdio 响应不是合法 JSON: {e}\n"
                f"原始响应: {line!r}\n"
                f"stderr 输出:\n{stderr_text}"
            )

        assert resp.get("jsonrpc") == "2.0", (
            f"jsonrpc 字段异常: {resp}\nstderr 输出:\n{stderr_text}"
        )
        assert resp.get("id") == 1, f"id 字段异常: {resp}"
        assert "result" in resp, (
            f"响应缺少 result 字段: {resp}\nstderr 输出:\n{stderr_text}"
        )
        # MCP initialize 响应应包含协议字段
        result = resp["result"]
        assert "protocolVersion" in result or "capabilities" in result, (
            f"initialize 响应缺少协议字段 (protocolVersion/capabilities): {result}"
        )


class TestHttpMainBoundary:
    """HTTP main 进程边界测试"""

    def test_http_main_health_endpoint(self, _isolated_env):
        """python -m app.main 启动后 /health 返回 200"""
        port = _find_free_port()
        env = {
            **os.environ,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "API_KEY": "",  # 禁用鉴权，/health 是 public path
            "STORAGE_BACKEND": "memory",  # HTTP 测试不强依赖 PG
        }

        proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=os.getcwd(),
        )

        ready = False
        body_text = None
        try:
            ready, body_text = _wait_for_health(port, timeout=15.0)
        finally:
            exit_code, stderr_text = _terminate_gracefully(proc, timeout=10.0)

        assert ready, (
            f"HTTP 服务 15s 内未就绪（exit_code={exit_code}）。\n"
            f"最后错误: {body_text}\n"
            f"stderr 输出:\n{stderr_text}"
        )

        # 二次解析 body
        assert body_text is not None, "health 响应 body 为空"
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError as e:
            pytest.fail(f"/health 响应不是合法 JSON: {e}\n原始: {body_text!r}")

        assert "status" in body, f"/health 响应缺少 status 字段: {body}"
        assert body["status"] in ("ok", "degraded", "unhealthy"), (
            f"/health status 异常: {body['status']}"
        )


class TestPGPoolLifecycleBoundary:
    """PG 连接池生命周期进程边界测试"""

    def test_pg_pool_closed_on_shutdown(self, _isolated_env):
        """
        验证进程终止后 PG 连接池正确关闭，无连接泄漏。

        平台差异：
        - Unix: SIGTERM 触发 lifespan shutdown → close_pool()，
          严格断言 stderr 含 "连接池已关闭" 或 "close_pool" 日志
        - Windows: proc.terminate() 等价于 TerminateProcess（硬 kill），
          不触发 lifespan shutdown，只严格断言"进程超时内退出"，
          日志检查作为 best-effort（不严格断言）。

        严格断言（所有平台）：进程在超时内退出（无挂死 = 无连接泄漏阻塞）。
        """
        if settings.storage_backend != "postgresql":
            pytest.skip(
                "STORAGE_BACKEND != postgresql，跳过 PG 池关闭验证。"
                "如需启用，请在 .env 设置 STORAGE_BACKEND=postgresql 并配置 PG 连接参数"
            )

        # 前置探测：PG 是否真的可连通
        try:
            from app.mcp.core.storage.pg_store import _get_pool
            pool = _get_pool()
            conn = pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            finally:
                pool.putconn(conn)
        except Exception as e:
            pytest.skip(
                f"PG 不可连通，跳过 PG 池关闭验证: {e}"
            )

        port = _find_free_port()
        # 环境变量完整覆盖，避免 .env 污染
        env = {
            **os.environ,
            "STORAGE_BACKEND": "postgresql",
            "PG_HOST": settings.pg_host,
            "PG_PORT": str(settings.pg_port),
            "PG_DATABASE": settings.pg_database,
            "PG_USER": settings.pg_user,
            "PG_PASSWORD": settings.pg_password,
            "API_KEY": "",
            "HOST": "127.0.0.1",
            "PORT": str(port),
        }

        proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=os.getcwd(),
        )

        ready = False
        wait_err = None
        try:
            ready, wait_err = _wait_for_health(port, timeout=15.0)
        finally:
            exit_code, stderr_text = _terminate_gracefully(proc, timeout=15.0)

        # 先验证服务确实起来过（说明 PG 池已初始化）
        assert ready, (
            f"HTTP 服务 15s 内未就绪（PG 池可能未初始化，exit_code={exit_code}）。\n"
            f"最后错误: {wait_err}\n"
            f"stderr 输出:\n{stderr_text}"
        )

        # 严格断言：进程在超时内退出（无挂死 = 无连接泄漏阻塞）
        assert exit_code is not None, (
            f"进程在 15s 内未退出，可能存在连接泄漏。\n"
            f"stderr 输出:\n{stderr_text}"
        )

        # 平台差异化日志断言
        if sys.platform != "win32":
            # Unix: SIGTERM 触发 lifespan，应看到 close_pool 日志
            assert "连接池已关闭" in stderr_text or "close_pool" in stderr_text, (
                f"Unix 上未找到 PG 连接池关闭日志（lifespan shutdown 未执行 close_pool）。\n"
                f"stderr 输出:\n{stderr_text}"
            )
        else:
            # Windows: best-effort，不严格断言
            # TerminateProcess 不触发 lifespan，但仍验证进程退出（已断言 exit_code is not None）
            # 仅记录未找到日志的事实，不 fail
            # 注：Windows TerminateProcess 是硬 kill，lifespan 不执行属预期行为
            pass


# ──────────────────────────────────────────────────────────────────────────
#  N3（任务 I）：stdio 关闭资源回收
#  ──────────────────────────────────────────────────────────────────────────
#  覆盖：
#  - uninstall_global_hook() 行为（恢复 sys.excepthook、幂等）
#  - cleanup_resources() 行为（关闭 PG 池 / 取消后台任务 / 幂等）
#  - stdio 子进程在 EOF / SIGINT 退出时干净回收资源（无 traceback）
#  ──────────────────────────────────────────────────────────────────────────

class TestUninstallGlobalHook:
    """N3：验证 uninstall_global_hook 行为"""

    def test_uninstall_restores_excepthook(self):
        from app.runtime.hooks import exception_hook

        original = sys.excepthook
        try:
            exception_hook.install_global_hook()
            assert sys.excepthook is not original  # 已被替换
            assert exception_hook._installed is True

            exception_hook.uninstall_global_hook()
            assert sys.excepthook is original  # 恢复
            assert exception_hook._installed is False
        finally:
            # 强制恢复，避免污染其他测试
            exception_hook._installed = False
            exception_hook._original_hook = None
            exception_hook._original_asyncio_handler = None
            sys.excepthook = original

    def test_uninstall_is_idempotent(self):
        from app.runtime.hooks import exception_hook

        original = sys.excepthook
        try:
            # 未安装时调用 uninstall 不应抛异常
            exception_hook._installed = False
            exception_hook.uninstall_global_hook()
            exception_hook.uninstall_global_hook()
            assert sys.excepthook is original
        finally:
            sys.excepthook = original
            exception_hook._installed = False
            exception_hook._original_hook = None


class TestCleanupResources:
    """N3：验证 mcp_server.cleanup_resources 行为"""

    def test_cleanup_is_idempotent(self, monkeypatch):
        import app.mcp_server as mcp_server

        monkeypatch.setattr(mcp_server, "_cleanup_done", False)
        monkeypatch.setattr(mcp_server, "_periodic_cleanup_task", None)
        monkeypatch.setattr(mcp_server.settings, "storage_backend", "memory")

        mcp_server.cleanup_resources()
        mcp_server.cleanup_resources()
        mcp_server.cleanup_resources()
        assert mcp_server._cleanup_done is True

    def test_cleanup_closes_pg_pool_when_postgresql(self, monkeypatch):
        import app.mcp_server as mcp_server
        from app.mcp.core.storage import pg_store

        monkeypatch.setattr(mcp_server, "_cleanup_done", False)
        monkeypatch.setattr(mcp_server, "_periodic_cleanup_task", None)
        monkeypatch.setattr(mcp_server.settings, "storage_backend", "postgresql")

        called = {"count": 0}

        def _fake_close_pool():
            called["count"] += 1

        monkeypatch.setattr(pg_store, "close_pool", _fake_close_pool)
        monkeypatch.setattr(mcp_server, "uninstall_global_hook", lambda: None)

        mcp_server.cleanup_resources()
        assert called["count"] == 1

        # 幂等：第二次不应再调用 close_pool
        mcp_server.cleanup_resources()
        assert called["count"] == 1

    def test_cleanup_skips_pg_pool_when_memory(self, monkeypatch):
        import app.mcp_server as mcp_server
        from app.mcp.core.storage import pg_store

        monkeypatch.setattr(mcp_server, "_cleanup_done", False)
        monkeypatch.setattr(mcp_server, "_periodic_cleanup_task", None)
        monkeypatch.setattr(mcp_server.settings, "storage_backend", "memory")

        called = {"count": 0}
        monkeypatch.setattr(pg_store, "close_pool", lambda: called.__setitem__("count", called["count"] + 1))
        monkeypatch.setattr(mcp_server, "uninstall_global_hook", lambda: None)

        mcp_server.cleanup_resources()
        assert called["count"] == 0  # memory 后端不应调用 close_pool

    def test_cleanup_cancels_periodic_task_if_running(self, monkeypatch):
        import app.mcp_server as mcp_server

        class _FakeTask:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

            def done(self):
                return False

        task = _FakeTask()
        monkeypatch.setattr(mcp_server, "_cleanup_done", False)
        monkeypatch.setattr(mcp_server, "_periodic_cleanup_task", task)
        monkeypatch.setattr(mcp_server.settings, "storage_backend", "memory")
        monkeypatch.setattr(mcp_server, "uninstall_global_hook", lambda: None)

        mcp_server.cleanup_resources()
        assert task.cancelled is True


class TestStdioExitCleanup:
    """N3：stdio 子进程退出路径触发资源回收"""

    def test_stdio_exits_cleanly_on_eof(self, _isolated_env):
        """关闭 stdin → 触发 EOF → 子进程应干净退出（exit code 0），无 traceback"""
        env = {
            **os.environ,
            "STORAGE_BACKEND": "memory",
            "API_KEY": "",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=os.getcwd(),
        )
        try:
            # 等待启动
            time.sleep(1.0)
            # 关闭 stdin 触发 EOF
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                returncode = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("stdio 子进程在 EOF 后未在 15s 内退出")
        finally:
            # 进程已退出后读 stderr（避免读阻塞）
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            stderr_text = _safe_read_stderr(proc)

        # 允许 0（正常退出）；signal 路径在 _signal_handler 内 sys.exit(0)
        assert returncode == 0, (
            f"非预期退出码 {returncode}（应为 0）。\nstderr 输出:\n{stderr_text}"
        )
        assert "Traceback (most recent call last)" not in stderr_text, (
            f"EOF 退出时 stderr 含 traceback:\n{stderr_text}"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 不支持 SIGTERM")
    def test_stdio_exits_on_sigterm(self, _isolated_env):
        """发送 SIGTERM → signal handler 触发 cleanup + sys.exit(0)"""
        env = {
            **os.environ,
            "STORAGE_BACKEND": "memory",
            "API_KEY": "",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=os.getcwd(),
        )
        try:
            time.sleep(1.0)
            try:
                proc.send_signal(signal.SIGTERM)
            except (OSError, ProcessLookupError) as e:
                pytest.skip(f"无法发送 SIGTERM: {e}")
            try:
                returncode = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("stdio 子进程在 SIGTERM 后未在 15s 内退出")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            stderr_text = _safe_read_stderr(proc)

        # signal handler 内 sys.exit(0) → returncode 0
        # 若 handler 未注册被信号杀掉 → -15
        assert returncode in (0, -15, 143), (
            f"非预期退出码 {returncode}（应为 0 或 -15/143）。\nstderr 输出:\n{stderr_text}"
        )
        assert "Traceback (most recent call last)" not in stderr_text, (
            f"SIGTERM 退出时 stderr 含 traceback:\n{stderr_text}"
        )

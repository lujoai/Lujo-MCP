"""heavy worker 统一启动器（DESIGN_C2 §1.1/§2.3/§3/§3.1/§3.2，B08 前置 W2-1）。

源码与冻结模式共用的父侧启动件：``subprocess.Popen`` 全参数、每尝试独立的
子→父结果通道（句柄所有权规则）、单一结果读取器、GO_COMMITTED 提交点与
写入生命周期（R05/R12）、握手三条失败路径与故障注入边界。**本模块只依赖
stdlib**，因此 worker 入口（heavy_worker_entry / 冻结 entry_stdio 分流）可以
安全复用本模块的结果通道接管与帧协议函数而不触发任何 app 服务器副作用。

结果通道所有权（§1.1，逐条硬约束）：

- **每尝试独立**一条子→父管道；父→子请求与 go 只走 stdin，ready 与结果只走
  结果通道；
- **stdout 出生即 DEVNULL**（Popen 参数，源码与冻结一致），不得等 handler
  导入后才重定向；stderr 继承父进程（None），不 PIPE；
- Windows 传**原生 HANDLE**（``_winapi`` 创建 + 可继承副本经
  ``startupinfo.lpAttributeList['handle_list']`` 进入子进程，CPython 会把
  std 句柄并入同一继承白名单），子进程经 ``msvcrt.open_osfhandle`` 做
  **HANDLE→CRT fd 接管**——禁止把 HANDLE 当 fd、禁止 dup2 到固定 fd 3；
  POSIX 经 ``pass_fds`` 传写 fd；
- **父侧创建成功即关自身写端副本**（原始写端在复制后立即关，可继承副本在
  Popen 成功后立即关——写端副本不关，父侧读端永远等不到 EOF）；失败路径在
  except 分支收口全部已取得资源；
- **子侧接管后立即清除写端可继承属性**（:func:`takeover_result_write_end`），
  且必须发生在任何 handler 导入或孙进程创建之前；
- **单一最终关闭者**：写端归子进程的接管 fd（worker 写完结果帧后关闭），
  读端归父侧 :class:`ResultChannel`。

单一读取器（§3.2）：每个结果管道只有一个读取器线程，依次解析 ready（单字节
``R``）与结果帧（8 字节小端长度 + 体），向控制路径发布事件；父侧握手代码
不得直接读管道。EOF 不等于「读取器线程已结束」——线程存活状态由真实 join
结果记录（C4 §2.2）；错误阶段字节、无效长度、截断帧均为协议错误，绝不当作
成功。

GO 写入生命周期（§2.3，R05）：逻辑提交（GO_COMMITTED）与管道字节写出是两个
事件。提交决策经 ``allow_commit`` 回调（W3 起由注册表锁内四条件给出：当前
尝试有效、未 closing、未 kill_due、实际终止后端已确认），提交置位后**不回
退**；随后锁外使用原尝试的专属 stdin 写 ``G`` 并 flush；提交先于终止请求时
**不能承诺业务绝未开始**；go 写出失败**禁止重试业务**；取消不得等待无期限
阻塞的 write（取消方不 join 写线程，写线程有界自限）。
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import time

# ── 协议常量（§3.2，父子两侧唯一权威定义，防第二次漂移） ─────────────────
READY_BYTE = b"R"
GO_BYTE = b"G"
_HEADER_STRUCT = struct.Struct("<Q")  # 8 字节无符号小端长度
RESULT_HEADER_LEN = _HEADER_STRUCT.size
_MAX_FRAME_BYTES = 64 * 1024 * 1024  # 无效长度防线：超出即协议错误

# 结果通道句柄传递所用的环境变量（Windows 传原生 HANDLE 值；POSIX 传 fd 号）
_ENV_RESULT_HANDLE = "LUJO_HEAVY_RESULT_HANDLE"
_ENV_RESULT_FD = "LUJO_HEAVY_RESULT_FD"

# Windows Popen 参数（§1.1）：新进程组使 CTRL_BREAK 成为协作级终止手段
_CREATE_NEW_PROCESS_GROUP = 0x200 if sys.platform == "win32" else 0

# 关闭读端时等待读取器线程退出的上界；超时只如实记录 alive，不谎报结束
_READER_JOIN_TIMEOUT = 2.0


class HeavySpawnBroken(Exception):
    """② 管道断裂/协议错误（ready/go 通道读写异常、分帧或阶段校验失败）。

    调用方映射 TOOL_INTERNAL（"heavy worker handshake broken"）；go 之后结果
    通道断裂且进程已退出的情形由 :class:`WorkerExitedWithoutResult` 表达。
    """


class HeavyHandshakeTimeout(Exception):
    """③ 握手超时：ready/结果在剩余预算内未到且进程仍存活。

    调用方映射 TOOL_TIMEOUT 口径（reason=handshake，C2 §3 ③）。
    """


class WorkerExitedBeforeReady(Exception):
    """① go 前崩溃：proc 已退出且 ready 永不到达（区别于 R6 的运行后退出）。"""

    def __init__(self, exitcode: int | None):
        super().__init__(f"heavy worker exited before ready (exitcode={exitcode})")
        self.exitcode = exitcode


class WorkerExitedWithoutResult(Exception):
    """go 之后、结果帧之前退出：归入既有「exited without result」语义。"""

    def __init__(self, exitcode: int | None):
        super().__init__(f"heavy tool exited without result (exitcode={exitcode})")
        self.exitcode = exitcode


class CommitRefused(Exception):
    """提交决策拒绝（kill_due/closing/后端未确认）：go 永不写出，调用方走终止。"""


# ── 结果通道创建（父侧）与接管（子侧） ──────────────────────────────────


def _create_result_channel_pair_windows() -> tuple[int, int, int]:
    """Windows：创建管道，返回 (读 fd, 可继承写句柄, 其句柄值)。

    原始写端在复制出可继承副本后**立即关闭**（父侧从不写结果通道）；可继承
    副本经 Popen ``lpAttributeList['handle_list']`` 进入子进程，句柄值不变
    （CreateProcess 句柄继承语义），经环境变量告知子进程。
    """
    import _winapi
    import msvcrt

    read_handle, write_handle = _winapi.CreatePipe(None, 0)
    inheritable_write = _winapi.DuplicateHandle(
        _winapi.GetCurrentProcess(),
        write_handle,
        _winapi.GetCurrentProcess(),
        0,
        True,  # bInheritHandle
        _winapi.DUPLICATE_SAME_ACCESS,
    )
    _winapi.CloseHandle(write_handle)  # 父侧原始写端：复制即关
    handle_value = int(inheritable_write)
    read_fd = msvcrt.open_osfhandle(read_handle, os.O_RDONLY | os.O_BINARY)
    return read_fd, inheritable_write, handle_value


def takeover_result_write_end() -> int:
    """子进程侧：从环境变量接管结果写端，返回二进制写 fd。

    Windows：读 HANDLE 值 → ``msvcrt.open_osfhandle`` 建 CRT fd（HANDLE→CRT fd
    接管，不是把 HANDLE 当 fd，也不经固定 fd 3）；POSIX：直接读 fd 号。
    接管后**立即清除可继承属性**——孙进程不得持有结果写端；本函数必须在任何
    handler 导入或孙进程创建之前调用。
    """
    if sys.platform == "win32":
        import msvcrt

        raw = os.environ.get(_ENV_RESULT_HANDLE)
        if not raw:
            raise OSError("result channel handle not provided to worker")
        fd = msvcrt.open_osfhandle(int(raw), os.O_WRONLY | os.O_BINARY)
    else:
        raw = os.environ.get(_ENV_RESULT_FD)
        if not raw:
            raise OSError("result channel fd not provided to worker")
        fd = int(raw)
    os.set_inheritable(fd, False)
    return fd


def close_result_write_end(fd: int) -> None:
    """子进程侧写端唯一最终关闭者的关闭入口（worker 写完结果帧后调用一次）。"""
    os.close(fd)


def write_frame(stream_or_fd, payload: bytes, *, header_len: int) -> None:
    """写一帧：可选长度前缀 + 体；流写后立即 flush（缓冲提交，§3.2）。

    - ready/go：``header_len=0``，payload 为单字节；
    - 结果帧/请求帧：``header_len=RESULT_HEADER_LEN``，payload 为完整字节串。
    """
    buf = b""
    if header_len:
        buf += _HEADER_STRUCT.pack(len(payload))
    buf += payload
    if hasattr(stream_or_fd, "write"):
        stream_or_fd.write(buf)
        stream_or_fd.flush()
    else:
        view = memoryview(buf)
        while view:
            written = os.write(stream_or_fd, view)
            view = view[written:]


def read_request_frame(stream) -> bytes:
    """子进程侧：从 stdin 读一帧请求（8 字节小端长度 + 原始字节）。

    go 前只读**原始字节**，不做 ``pickle.loads``、不导入 handler（C2 §1.1）。
    """
    header = _read_exactly(stream, RESULT_HEADER_LEN)
    (length,) = _HEADER_STRUCT.unpack(header)
    if length > _MAX_FRAME_BYTES:
        raise OSError(f"request frame length {length} exceeds limit")
    return _read_exactly(stream, length)


def _read_exactly(stream, size: int) -> bytes:
    """阻塞流上的 read-exactly：处理短读；EOF 抛 EOFError（由阶段归类）。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("result channel EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ── 单一结果读取器（§3.2） ──────────────────────────────────────────────


class ResultChannel:
    """每尝试独立的结果通道读端 + 唯一读取器线程。

    读取器依次期待：单字节 ``R``（ready）→ 8 字节长度 + 结果体。任一阶段
    EOF 都如实发布；错误阶段字节、重复 ready 位形、超出上限的无效长度、
    截断帧均发布**协议错误**，绝不当作成功。父侧握手代码不得直接读管道。
    """

    def __init__(self, read_file):
        self._file = read_file
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self.ready = threading.Event()
        self.result_ready = threading.Event()
        self.result_bytes: bytes | None = None
        self.error: str | None = None
        self.eof_seen = False
        self.reader_alive: bool | None = None  # None=未关闭；真实 join 结果
        self.closed = False

    def start_reader(self) -> None:
        with self._lock:
            if self._reader is not None:
                return
            self._reader = threading.Thread(
                target=self._read_loop, name="lujo-heavy-result-reader", daemon=True
            )
            self._reader.start()

    def reader_thread_dead(self) -> bool:
        """读取器线程是否已真实结束（基于 is_alive 观察并记录，非 EOF 推断）。"""
        reader = self._reader
        if reader is None or reader.is_alive():
            return False
        self.reader_alive = False
        return True

    def _fail(self, message: str) -> None:
        with self._lock:
            if self.error is None:
                self.error = message
        self.result_ready.set()

    def _read_loop(self) -> None:
        try:
            first = self._file.read(1)
            if not first:
                with self._lock:
                    self.eof_seen = True
                self.result_ready.set()
                return
            if first != READY_BYTE:
                self._fail(f"handshake protocol error: bad ready byte {first!r}")
                return
            self.ready.set()
            header = _read_exactly(self._file, RESULT_HEADER_LEN)
            (length,) = _HEADER_STRUCT.unpack(header)
            if length > _MAX_FRAME_BYTES:
                self._fail(f"handshake protocol error: invalid frame length {length}")
                return
            body = _read_exactly(self._file, length)
            with self._lock:
                self.result_bytes = body
            self.result_ready.set()
        except EOFError:
            with self._lock:
                self.eof_seen = True
            self.result_ready.set()
        except OSError as exc:
            self._fail(f"handshake broken on result channel: {exc}")
        finally:
            # 关闭权归 ResultChannel.close()（父侧读端唯一最终关闭者）
            pass

    def close(self) -> None:
        """父侧读端唯一最终关闭者：关文件，并用真实 join 结果记录线程存活。"""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        try:
            self._file.close()
        except OSError:
            pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=_READER_JOIN_TIMEOUT)
            # C4 §2.2：不得声称「通道 EOF 必然意味着读取线程已结束」——
            # alive 由真实 join 结果记录，超时即为 True。
            self.reader_alive = reader.is_alive()
        else:
            self.reader_alive = False


# ── 启动的尝试（Popen 全参数 + GO 写入生命周期） ────────────────────────


class SpawnedAttempt:
    """一次进程尝试的父侧资源束：Popen、专属 stdin 引用、结果通道与 go 状态。"""

    def __init__(self, attempt_id: int, proc: subprocess.Popen, result: ResultChannel):
        self.attempt_id = attempt_id
        self.proc = proc
        self.result = result
        # 原尝试的专属 stdin 引用（§2.3：旧写入只作用于原尝试资源）
        self._stdin = proc.stdin
        self._go_lock = threading.Lock()
        self.go_state = "NONE"  # NONE → COMMITTED → WRITTEN / WRITE_FAILED
        self.request_write_error: str | None = None

    # -- 请求帧（父→子，线程化：大请求/不读 stdin 都不得阻塞截止判定，§3.2） --

    def write_request_async(self, request_bytes: bytes) -> threading.Event:
        done = threading.Event()

        def _run() -> None:
            try:
                write_frame(self._stdin, request_bytes, header_len=RESULT_HEADER_LEN)
            except (OSError, ValueError) as exc:
                # 写失败由握手等待方按 ② 归类（§3.1 边界 1）
                self.request_write_error = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        threading.Thread(target=_run, name="lujo-heavy-req-writer", daemon=True).start()
        return done

    # -- GO_COMMITTED 提交点与写入生命周期（§2.3，R05） --

    def commit_go(self, allow_commit) -> None:
        """在提交锁内做逻辑提交决策；拒绝抛 :class:`CommitRefused`。

        ``allow_commit`` 由调用方提供（注册表锁内检查四条件后给出）。提交
        置位后**不回退**；重复提交幂等，绝不重复决策。
        """
        with self._go_lock:
            if self.go_state != "NONE":
                return
            if not allow_commit():
                raise CommitRefused("go commit refused by caller gate")
            self.go_state = "COMMITTED"

    def write_go_bounded(self, timeout: float = 5.0) -> None:
        """锁外写出 G（原尝试专属 stdin）并 flush；等待有界，不无期限阻塞。

        写出失败置 WRITE_FAILED（GO_COMMITTED 已登记，**禁止重试业务**）；
        超过 timeout 的阻塞写被有界放弃——写线程随管道可写或进程死亡自然
        消亡，取消路径绝不等待它。
        """
        written = threading.Event()
        error: list[OSError | ValueError] = []

        def _run() -> None:
            try:
                write_frame(self._stdin, GO_BYTE, header_len=0)
            except (OSError, ValueError) as exc:
                error.append(exc)
            finally:
                written.set()

        writer = threading.Thread(target=_run, name="lujo-heavy-go-writer", daemon=True)
        writer.start()
        if not written.wait(timeout):
            return  # 有界放弃；提交状态保持 COMMITTED（不能承诺业务绝未开始）
        if error:
            with self._go_lock:
                self.go_state = "WRITE_FAILED"
            raise HeavySpawnBroken(f"go write failed: {error[0]}")
        with self._go_lock:
            self.go_state = "WRITTEN"

    def abandon_pending_go_writes(self, timeout: float = 2.0) -> None:
        """取消路径锚点：取消方**不等待**阻塞中的 write（§2.3）。

        go 写线程自身有界（write_go_bounded），取消方在此只承诺「不等待」：
        本方法立即返回，不做任何无期限 join。
        """
        return None


def spawn_attempt(
    attempt_id: int,
    command: list[str],
    env: dict | None = None,
    cwd: str | None = None,
    *,
    extra_creationflags: int = 0,
    start_new_session: bool = False,
) -> SpawnedAttempt:
    """按 §1.1 全参数启动一个 worker 尝试，创建其独立结果通道。

    ``extra_creationflags``：Windows 附加 creationflags（如
    CREATE_BREAKAWAY_FROM_JOB / CREATE_NO_WINDOW——C3 §2.1 降级链与能力快照）；
    ``start_new_session``：POSIX 新会话/新进程组（pgid==pid，终止后端按出生
    pgid 两级 killpg，W3-2）。失败路径（Popen 抛错等）：收口全部已取得资源
    （读端文件、可继承写句柄），不留半开句柄。
    """
    child_env = dict(os.environ if env is None else env)
    read_file = None
    parent_write_objects: list = []
    popen_kwargs: dict = {}

    try:
        if sys.platform == "win32":
            read_fd, inheritable_write, handle_value = _create_result_channel_pair_windows()
            read_file = os.fdopen(read_fd, "rb", buffering=0)
            child_env[_ENV_RESULT_HANDLE] = str(handle_value)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpAttributeList = {"handle_list": [inheritable_write]}
            popen_kwargs["startupinfo"] = startupinfo
            popen_kwargs["close_fds"] = True
            parent_write_objects = [inheritable_write]
        else:
            read_fd, write_fd = os.pipe()
            read_file = os.fdopen(read_fd, "rb", buffering=0)
            child_env[_ENV_RESULT_FD] = str(write_fd)
            popen_kwargs["pass_fds"] = (write_fd,)
            popen_kwargs["start_new_session"] = start_new_session
            parent_write_objects = [write_fd]

        proc = subprocess.Popen(  # noqa: S603 —— 固定命令行，非用户输入
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,  # 出生即隔离 fd1（§1.1）
            stderr=None,  # stderr 继承：日志通道，不进结果流
            cwd=cwd if cwd is not None else os.getcwd(),
            env=child_env,
            creationflags=_CREATE_NEW_PROCESS_GROUP | extra_creationflags,
            **popen_kwargs,
        )
    except BaseException:
        if read_file is not None:
            try:
                read_file.close()
            except OSError:
                pass
        for obj in parent_write_objects:
            _close_parent_write_object(obj)
        raise

    # 成功：父侧创建成功即关自身写端副本（§1.1）——不关则读端永远等不到 EOF；
    # 读端此后归 ResultChannel 唯一持有。
    for obj in parent_write_objects:
        _close_parent_write_object(obj)
    return SpawnedAttempt(attempt_id, proc, ResultChannel(read_file))


def _close_parent_write_object(obj) -> None:
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CloseHandle(obj)
        else:
            os.close(obj)
    except OSError:
        pass


# ── 握手（§2.1 ①②③⑥ / §3 失败路径归类） ───────────────────────────────


def handshake(
    attempt: SpawnedAttempt,
    request_bytes: bytes,
    deadline: float,
    allow_commit,
) -> bytes:
    """执行完整握手直到拿到结果体字节；所有失败路径归类为 §3 的类型化异常。

    顺序（R12）：① 先启动唯一结果读取器 → ② 非阻塞启动请求写入 →
    ③ 只等读取器发布的 ready 事件（同时监视请求写失败 / 协议错误 / go 前退
    出 / 截止）→ ⑥ 提交决策（allow_commit）→ 锁外写 G → 等结果帧。任何阶段
    都不直接读结果管道（单一读取器纪律）。
    """
    attempt.result.start_reader()
    attempt.write_request_async(request_bytes)

    def remaining() -> float:
        return deadline - time.monotonic()

    # ③ 等 ready（或失败归类）
    while not attempt.result.ready.wait(timeout=min(max(remaining(), 0.0), 0.05)):
        if attempt.request_write_error is not None:
            raise HeavySpawnBroken(f"request frame write failed: {attempt.request_write_error}")
        if attempt.result.error is not None:
            raise HeavySpawnBroken(attempt.result.error)
        if attempt.result.result_ready.is_set():
            # EOF 在 ready 之前发布：按进程状态区分 ① 与 ②
            if attempt.proc.poll() is not None:
                attempt.result.close()
                raise WorkerExitedBeforeReady(attempt.proc.returncode)
            raise HeavySpawnBroken("result channel EOF before ready")
        if attempt.proc.poll() is not None:
            # 进程已退出：给在途 ready 一拍宽限再归类，避免 poll 与读取器竞态
            if attempt.result.ready.wait(0.2):
                break
            attempt.result.close()
            raise WorkerExitedBeforeReady(attempt.proc.returncode)
        if attempt.result.reader_thread_dead():
            raise HeavySpawnBroken("result reader thread died before ready")
        if remaining() <= 0:
            raise HeavyHandshakeTimeout(
                f"heavy worker ready not observed within budget ({attempt.proc.pid})"
            )
    if attempt.result.error is not None:
        raise HeavySpawnBroken(attempt.result.error)

    # ⑥ 提交决策（拒绝 → CommitRefused，go 永不写出）→ 锁外写 G
    attempt.commit_go(allow_commit)
    attempt.write_go_bounded()

    # 等结果帧（或 go 后退出）
    while not attempt.result.result_ready.wait(timeout=min(max(remaining(), 0.0), 0.05)):
        if attempt.result.error is not None:
            raise HeavySpawnBroken(attempt.result.error)
        if attempt.proc.poll() is not None:
            if attempt.result.result_bytes is not None:
                break
            # 给在途结果一拍宽限再归类（读取器可能仍在收尾）
            if attempt.result.result_ready.wait(0.2):
                continue
            attempt.result.close()
            raise WorkerExitedWithoutResult(attempt.proc.returncode)
        if attempt.result.reader_thread_dead():
            raise HeavySpawnBroken("result reader thread died before result")
        if remaining() <= 0:
            raise HeavyHandshakeTimeout(
                f"heavy worker result not observed within budget ({attempt.proc.pid})"
            )
    if attempt.result.error is not None:
        raise HeavySpawnBroken(attempt.result.error)
    if attempt.result.result_bytes is None:
        # EOF 且无结果体：先等退出状态收敛（EOF 事件可能先于 waitable 退出），
        # 进程已退出按「exited without result」归类；进程仍存活则属 ②，
        # 由调用方走终止升级（发出终止 ≠ 已退出）。
        try:
            attempt.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        if attempt.proc.poll() is not None:
            raise WorkerExitedWithoutResult(attempt.proc.returncode)
        raise HeavySpawnBroken("result channel EOF without result frame")
    return attempt.result.result_bytes


def terminate_and_reap(
    attempt: SpawnedAttempt, grace: float = 5.0, self_exit_grace: float = 0.5
) -> int | None:
    """终止并回收尝试：等自然退出（宽限）→ terminate → kill → 关资源（幂等）。

    仅做资源回收；「确认退出 → REAPED → 结算」的记账属于注册表层（C1/W3），
    本函数不结算任何许可。
    """
    proc = attempt.proc
    if proc.poll() is None:
        # 先给自然退出一个短宽限（结果送达后 worker 应自行收尾退出），
        # 仍存活才升级 terminate → kill，杜绝残留。
        try:
            proc.wait(timeout=self_exit_grace)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=grace)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except OSError:
            pass
    stdin = proc.stdin
    if stdin is not None and not stdin.closed:
        try:
            stdin.close()
        except OSError:
            pass
    attempt.result.close()
    return proc.returncode

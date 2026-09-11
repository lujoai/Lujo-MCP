"""退出编排（DESIGN_C4 §1、§2、§3；B15 / W4-1，看门狗 W4-2、信号 W4-3、
①–⑥ 接线 W4-4）。

模块职责与硬约束（逐条对齐）：

- **代际关闭与进程退出分账**：本模块的意图/看门狗只服务「进程退出」；
  普通 lifespan 结束**不调用** :func:`record_exit_intent`，不武装看门狗。
  进程退出所有权由独立入口显式持有（纯 stdio / 统一模式 / 独立 HTTP），
  **不通过「是否在 pytest 中」判断**。
- **退出意图拍点**：t0 = 本进程首次可靠观察退出意图的时刻；后续 EOF、
  信号、finally、清理请求**只合并原因，不刷新或延后 deadline**。
- **薄信号 handler**（:func:`signal_handler_stub`）：只做普通属性赋值
  （发布意图与首次触发时间）——**不调用** threading.Event.set、Condition、
  logging、asyncio 清理或项目锁（信号回调取锁可能死锁）。
- **EOF 感知流代理**（:func:`create_eof_aware_stdin_proxy`）：包装**唯一
  stdin 输入生产者**的底层流，在原始读取处观察 EOF 并一次性触发回调；
  不创建第二个读取线程、不做任何缓冲（无界转交不存在）。
- **看门狗**（:class:`ExitSupervisor`）：daemon 监督线程在独立入口接纳调用
  前创建并确认就绪；创建失败 → 启动失败；只观察退出状态与单调时间；
  deadline（t0 + 25s）到点**直接 os._exit(0)**——该调用之前禁止 logging /
  print / stderr.write / flush / 文件 I/O / Job 清理 / join / 取任何项目锁；
  监督线程内部异常不得静默吞掉（已武装后进入同一无阻塞最后防线）；
  进程退出意图一旦成立看门狗**不可撤销**（无 disarm）。
- **M1 ①–⑥ 序编排**（:func:`run_m1_sequence`）：顺序固定
  （停止接纳 → 取消等待者 → 终止在途 → 关 Job → 关池 → 幂等），池关闭
  不得先于进程终止；每步执行前检查剩余预算（子预算表）且**实际等待也必须
  有界**——超预算记录结构化事件后继续，不中断序列；看门狗独立计时兜底。
"""

from __future__ import annotations

import os
import threading
import time

# 绝对退出期限（秒，C4 §2.1；实现期只收紧不放宽）
EXIT_DEADLINE_SECONDS = 25.0

# 信号 → 退出原因名（§4 reason 枚举）
_SIGNAL_REASON = "signal"

# ── 退出意图（进程级；锁内正式记账；信号回调走无锁快路径） ────────────────

_intent_lock = threading.Lock()
_intent_t0: float | None = None
_intent_reasons: frozenset = frozenset()
_deadline: float | None = None

# 信号回调专用：普通属性赋值（GIL 原子），由正式记账路径采纳
_signal_t0: float | None = None
_signal_reason: str | None = None

# 监督线程时钟读数（独立钩子：故障演练可注入，不污染全局 time 模块）
_watch_now = time.monotonic


def signal_handler_stub(signum, frame) -> None:
    """薄信号 handler：只发布退出意图与首次触发时间（普通属性赋值）。

    不使用事件唤醒、条件变量、日志或任何锁（信号回调语境下取锁可能死锁）。
    t0 内联记录首次可靠观察时刻；正式记账路径会原样采纳。
    """
    global _signal_t0, _signal_reason
    if _signal_t0 is None:
        _signal_t0 = time.monotonic()
        _signal_reason = _SIGNAL_REASON


def record_exit_intent(reason: str) -> float:
    """正式记录进程退出意图；返回 t0。首次调用确定 deadline，后续只合并原因。"""
    global _intent_t0, _intent_reasons, _deadline
    with _intent_lock:
        if _intent_t0 is None:
            # 采纳信号回调的首次触发时间（若更早）；否则以当前时刻为 t0
            if _signal_t0 is not None:
                _intent_t0 = _signal_t0
                _intent_reasons = _intent_reasons | {_signal_reason}
            else:
                _intent_t0 = time.monotonic()
            _deadline = _intent_t0 + EXIT_DEADLINE_SECONDS
        _intent_reasons = _intent_reasons | {reason}
        return _intent_t0


def exit_intent_recorded() -> bool:
    with _intent_lock:
        return _intent_t0 is not None


def deadline_remaining() -> float | None:
    """距绝对 deadline 的剩余秒数；未记录意图时返回 None。"""
    with _intent_lock:
        if _deadline is None:
            return None
        return _deadline - time.monotonic()


# ── 看门狗（监督线程；W4-2 的实现体） ────────────────────────────────────


class ExitSupervisor:
    """进程级监督线程：观察退出意图与单调时间，deadline 到点无阻塞强退。

    只读取进程退出状态、检查单调时间并等待下一次检查；不执行工具、不接触
    项目锁 / 日志系统 / 业务事件循环。创建后必须确认线程就绪（``ready``），
    就绪失败向上传播为启动失败。
    """

    def __init__(self) -> None:
        self.ready = False
        self._wake = threading.Event()  # 意图通知（监督线程侧，非信号回调）
        self._stop = threading.Event()  # 仅测试/进程自然终了使用
        self._thread: threading.Thread | None = None

    @classmethod
    def create(cls) -> "ExitSupervisor":
        """创建并确认就绪；线程资源不可得 → 启动失败（不得继续接纳调用）。"""
        sup = cls()
        thread = threading.Thread(
            target=sup._watch, name="lujo-exit-watchdog", daemon=True
        )
        sup._thread = thread
        thread.start()
        deadline = time.monotonic() + 5.0
        while not sup.ready and time.monotonic() < deadline:
            time.sleep(0.005)
        if not sup.ready:
            raise RuntimeError("exit watchdog thread failed to become ready")
        return sup

    def record(self, reason: str) -> float:
        """记录退出意图（转发到正式记账）并唤醒监督线程。"""
        t0 = record_exit_intent(reason)
        self._wake.set()
        return t0

    @property
    def deadline(self) -> float | None:
        with _intent_lock:
            return _deadline

    def shutdown_watchdog(self) -> None:
        """进程自然终了路径的停止钩子（仅测试/解释器收尾使用；不影响已武装
        的最后防线——deadline 到点照常 os._exit，见测试口径）。"""
        self._stop.set()
        self._wake.set()

    def _watch(self) -> None:
        try:
            self.ready = True
            while True:
                _watch_now()  # 时钟读数失败 → 内部异常 → 同一最后防线
                if self._stop.is_set() and not exit_intent_recorded():
                    return
                if exit_intent_recorded():
                    remaining = deadline_remaining()
                    if remaining is None or remaining <= 0:
                        self._fired = True
                        os._exit(0)  # 无阻塞最后防线：之前禁止任何 IO/锁/join
                        return  # 生产中 os._exit 不返回；测试替身返回时线程收尾
                    # 到点前小步等待：唤醒（新原因）或到点（先查剩余再退）
                    if self._wake.wait(timeout=min(remaining, 0.1)):
                        self._wake.clear()
                    if deadline_remaining() is not None and deadline_remaining() <= 0:
                        self._fired = True
                        os._exit(0)
                        return
                    continue
                # 未武装：等待意图或停止信号（不消耗 CPU 自旋）
                if self._wake.wait(timeout=0.2):
                    self._wake.clear()
                    if self._stop.is_set():
                        return
        except BaseException:  # noqa: BLE001 —— 监督错误进入同一最后防线
            self._fired = True
            os._exit(0)
            return


# 独立入口的进程级监督者（每进程一个；ensure_exit_supervisor 幂等创建）
_supervisor: ExitSupervisor | None = None


def ensure_exit_supervisor() -> ExitSupervisor:
    """独立入口**接纳调用前**调用：创建并确认就绪（幂等）。

    创建失败向上传播 = 启动失败（不得吞掉后继续接纳调用）；已创建则返回
    同一实例（单监督线程）。嵌入式宿主（TestClient 等）**不得**调用本函数
    ——它们只有代际关闭权限（§1.1 分账）。
    """
    global _supervisor
    if _supervisor is None:
        _supervisor = ExitSupervisor.create()
    return _supervisor


# ── EOF 感知流代理（接在唯一 stdin 输入生产者上；透传，无第二读取线程） ───


def create_eof_aware_stdin_proxy(underlying, on_eof):
    """包装唯一 stdin 输入生产者的底层流：在原始读取处观察 EOF。

    - EOF（读到空）时**恰好一次**触发 ``on_eof``（记录退出意图）；数据透传，
      不缓存、不排队（无界转交不存在）；
    - 不创建任何读取线程——不存在第二个竞争读取 stdin 的线程；
    - 代理不吞 EOF：EOF 之后的读取继续返回空，由上层协议自然收口。
    """

    class _EofAwareProxy:
        def __init__(self, stream, callback):
            self._stream = stream
            self._callback = callback
            self._eof_fired = False

        def _fire_eof_once(self):
            if not self._eof_fired:
                self._eof_fired = True
                self._callback()

        def read(self, size=-1):
            data = self._stream.read(size)
            if not data:
                self._fire_eof_once()
            return data

        def readline(self, size=-1):
            data = self._stream.readline(size)
            if not data:
                self._fire_eof_once()
            return data

        def readinto(self, buffer):
            n = self._stream.readinto(buffer) if hasattr(self._stream, "readinto") else 0
            if not n:
                self._fire_eof_once()
            return n

        def __getattr__(self, name):  # 其余属性透传（缓冲层/关闭语义）
            return getattr(self._stream, name)

    return _EofAwareProxy(underlying, on_eof)


class _StdinShim:
    """sys.stdin 替身：text 层透传；``.buffer`` 为 EOF 感知二进制代理。"""

    def __init__(self, text, buffer):
        self._text = text
        self.buffer = buffer

    def __getattr__(self, name):
        return getattr(self._text, name)


def wrap_stdin_with_eof_awareness(stdin_text, on_eof):
    """把 sys.stdin 替换为带 EOF 感知 buffer 的替身（sys.stdin.buffer 只读，
    不可直接赋值——故整体替换 stdin，text 层原样透传）。"""
    return _StdinShim(stdin_text, create_eof_aware_stdin_proxy(stdin_text.buffer, on_eof))


# ── M1 ①–⑥ 序编排（§3；stdio cleanup 与 HTTP lifespan 共用同一实现） ─────

_M1_ORDER = (
    "step1_stop_accepting",
    "step2_cancel_waiters",
    "step3_terminate_active",
    "step4_close_jobs",
    "step5_shutdown_pools",
    "step6_b23_idempotent",
)

_M1_BUDGETS = {
    "step1_stop_accepting": 2.0,
    "step2_cancel_waiters": 2.0,
    "step3_terminate_active": 10.0,  # 并行硬上限（§2.1）
    "step4_close_jobs": 2.0,
    "step5_shutdown_pools": 2.0,
    "step6_b23_idempotent": 2.0,
}


def run_m1_sequence(
    *,
    t0: float,
    hooks: dict,
    over_budget_events,
    budget_overrides: dict | None = None,
) -> None:
    """按固定顺序执行 M1 ①–⑥；每步受子预算与剩余绝对时间双重约束。

    - 顺序固定：⑤ 关池**不得**先于 ③ 终止在途（先关池会让在途 reaper 失去
      载体且不杀进程）；
    - 每步执行**前**检查剩余预算，且步骤内部的实际等待也必须有界（由步骤
      实现保证）；实际等待超过子预算 → 记录结构化事件后继续（不中断序列），
      看门狗独立计时兜底；
    - ``hooks``：每步的可调用（无参）；缺失的步骤跳过（局部接线场景）。
    """
    overrides = budget_overrides or {}
    deadline = t0 + EXIT_DEADLINE_SECONDS
    for name in _M1_ORDER:
        hook = hooks.get(name)
        if hook is None:
            continue
        budget = min(overrides.get(name, _M1_BUDGETS[name]), max(deadline - time.monotonic(), 0.0))
        t_start = time.monotonic()
        hook()
        elapsed = time.monotonic() - t_start
        if elapsed > budget:
            over_budget_events({
                "step": name,
                "budget_s": round(budget, 3),
                "elapsed_s": round(elapsed, 3),
                "kind": "over_budget",
            })

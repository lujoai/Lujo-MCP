"""容量代际属主与 token 状态机（DESIGN C1 §3.4 / §5.1 / §5.2 的实现）。

设计要点（对应 docs/internal/DESIGN_C1_SLOT_ACCOUNTING.md）：

1. **任务结算、许可归还与代际退役是三个不同事件**。``call_soon_threadsafe``
   成功只表示回调已入队，**不表示许可已经归还**。
2. 每个成功取得的许可对应一个 :class:`SlotToken`，持有原始
   ``(generation, loop, semaphore)``，并依次处于
   ACTIVE → RETURN_PENDING → RETURNED / RETIRED。
3. :meth:`SlotPool.settle` 在锁内只完成 ACTIVE→RETURN_PENDING 并投递「带 token
   校验和确认记账的归还回调」；真正的 ``semaphore.release()`` 只在属主 loop 上
   执行。**工作线程不得直接操作 asyncio Semaphore。**
4. 代际守恒：``A = P + Q + R + X``。禁止用「强制归零」代替守恒证明；旧代退役时
   只把已结算的 RETURN_PENDING 转为 RETIRED，**不得把仍有真实执行的 ACTIVE 清零**。

本模块只依赖 stdlib 与 ``app.config``；**禁止** import server.py / mcp_server.py /
tools/*（防上层反向依赖）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "TokenState",
    "SlotToken",
    "GenerationCounters",
    "SlotPool",
    "ExecutorLifecycle",
    "SlotGenerationUnavailable",
    "lifecycle",
]


class SlotGenerationUnavailable(RuntimeError):
    """当前 loop 与旧代际不同，且旧代仍有在途 / 未决 token。

    DESIGN C1 §5（B20）：此时**禁止**把旧等待者迁移到新 semaphore
    （迁移＝两代容量叠加＝容量翻倍），调用方必须按「服务关闭中 / TOOL_BUSY」fast-fail。
    """


class TokenState(str, Enum):
    """许可 token 的状态（DESIGN C1 §3.4 状态表）。"""

    ACTIVE = "ACTIVE"
    RETURN_PENDING = "RETURN_PENDING"
    RETURNED = "RETURNED"
    RETIRED = "RETIRED"


@dataclass
class SlotToken:
    """一次成功取得的许可。持有原始 ``(generation, loop, semaphore)``。"""

    token_id: int
    generation: int
    loop: asyncio.AbstractEventLoop
    semaphore: asyncio.Semaphore
    state: TokenState = TokenState.ACTIVE


@dataclass
class GenerationCounters:
    """单代守恒计数（DESIGN C1 §5.1）。

    - ``A``：成功取得许可的累计次数（含获槽后补偿）
    - ``P``：ACTIVE token 数（尚未确认真实执行结束）
    - ``Q``：RETURN_PENDING token 数（执行已结束但许可尚未归还）
    - ``R``：已完成 RETURNED 的累计次数
    - ``X``：随旧代销毁而完成 RETIRED 的累计次数

    任意可观察的稳定状态必须满足 ``A == P + Q + R + X``。
    """

    generation: int
    A: int = 0
    P: int = 0
    Q: int = 0
    R: int = 0
    X: int = 0
    closing: bool = False
    retired: bool = False

    @property
    def balanced(self) -> bool:
        return self.A == self.P + self.Q + self.R + self.X

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "A": self.A,
            "P": self.P,
            "Q": self.Q,
            "R": self.R,
            "X": self.X,
            "closing": self.closing,
            "retired": self.retired,
        }


class SlotPool:
    """单个具名容量池：信号量 + 代际状态 + token 账目。

    线程安全：所有状态变更都在 ``self._lock`` 内完成。锁内**禁止**日志输出以外的
    阻塞动作；本类锁内不做 I/O、不做跨线程等待。
    """

    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = capacity
        self._lock = threading.Lock()
        self._next_token_id = 1
        self._generation = 1
        self._semaphore = asyncio.Semaphore(capacity)
        # 活动表：只保留 ACTIVE / RETURN_PENDING 的 token，避免随调用数永久增长
        self._tokens: dict[int, SlotToken] = {}
        self._counters: dict[int, GenerationCounters] = {
            1: GenerationCounters(generation=1)
        }
        # 归还回调投递失败的累计次数（仅诊断用；义务不丢，仍留在 Q）
        self._delivery_failures = 0
        # acquire 等待者登记表（B20 条件 4 的可观察面）：waiter_id -> (loop, task)。
        # 仅登记「正在等待 acquire 完成」的任务；取得 / 取消 / 超时后必须注销。
        self._waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self._next_waiter_id = 1
        # 关闭流程已请求取消的等待者（区别于调用方自身被取消）
        self._waiters_cancel_requested: set[int] = set()

    # ------------------------------------------------------------------ 只读

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def semaphore(self) -> asyncio.Semaphore:
        with self._lock:
            return self._semaphore

    @property
    def delivery_failures(self) -> int:
        with self._lock:
            return self._delivery_failures

    def counters(self, generation: int | None = None) -> GenerationCounters:
        with self._lock:
            gen = self._generation if generation is None else generation
            return self._counters[gen]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "capacity": self.capacity,
                "generation": self._generation,
                "counters": {g: c.as_dict() for g, c in self._counters.items()},
                "active_tokens": len(self._tokens),
                "delivery_failures": self._delivery_failures,
                "waiters": len(self._waiters),
            }

    def assert_conservation(self, generation: int | None = None) -> None:
        """校验 ``A == P + Q + R + X``；不成立即抛 AssertionError。"""
        with self._lock:
            gens = [self._generation] if generation is None else [generation]
            for g in gens:
                c = self._counters[g]
                if not c.balanced:
                    raise AssertionError(
                        f"代际 {g} 守恒被破坏：A={c.A} != P+Q+R+X="
                        f"{c.P + c.Q + c.R + c.X}（{c.as_dict()}）"
                    )

    # -------------------------------------------------------------- 取得许可

    def acquire_token(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> SlotToken:
        """在**已成功取得信号量**之后登记一个 ACTIVE token。

        调用方必须已经 ``await semaphore.acquire()`` 成功；本方法不负责 acquire，
        只负责记账（``A += 1``、``P += 1``）。

        ``semaphore`` 用于「按调用捕获实际被 acquire 的信号量对象」——归还回调必须
        释放**同一个对象**。传入时以调用方给出的为准（便于测试替换池实例），
        未传入时使用本池当前代际的信号量。
        """
        loop = loop or asyncio.get_running_loop()
        with self._lock:
            gen = self._generation
            counters = self._counters[gen]
            token = SlotToken(
                token_id=self._next_token_id,
                generation=gen,
                loop=loop,
                semaphore=semaphore if semaphore is not None else self._semaphore,
                state=TokenState.ACTIVE,
            )
            self._next_token_id += 1
            self._tokens[token.token_id] = token
            counters.A += 1
            counters.P += 1
            return token

    # ---------------------------------------------------------------- 结算

    def settle(self, token: SlotToken) -> bool:
        """结算入口：锁内只做 ACTIVE→RETURN_PENDING，并投递归还回调。

        可在任意线程调用（池工作线程回调、事件循环、closer）。

        返回 ``True`` 表示本次调用赢得了结算（第一次），``False`` 表示幂等空转。
        **返回 True 不等于许可已归还**——归还由属主 loop 上的回调完成。
        """
        with self._lock:
            current = self._tokens.get(token.token_id)
            if current is None or current.state is not TokenState.ACTIVE:
                return False
            current.state = TokenState.RETURN_PENDING
            counters = self._counters[current.generation]
            counters.P -= 1
            counters.Q += 1
            loop = current.loop

        # 锁外投递（禁止在锁内做跨线程动作）
        try:
            loop.call_soon_threadsafe(self._return_callback, current)
        except RuntimeError:
            # loop 已关闭或投递失败：token 保持 RETURN_PENDING，义务不丢；
            # 由代际属主在退役时把 Q 逐项转为 X（DESIGN C1 §3.4 情形表）
            with self._lock:
                self._delivery_failures += 1
            logger.warning(
                "容量池 %s：许可归还回调投递失败（token=%s, gen=%s），"
                "token 保持 RETURN_PENDING，将在代际退役时结清",
                self.name,
                token.token_id,
                token.generation,
            )
        return True

    def _return_callback(self, token: SlotToken) -> None:
        """归还回调：**只在属主 loop 上执行**。

        同一段不含 await 的操作内完成三件事：
        1. 校验 token 仍为 RETURN_PENDING，且原代尚未退役；
        2. 对原 semaphore 恰好 release 一次；
        3. 标记 RETURNED，更新归还计数并移出活动表。
        """
        with self._lock:
            current = self._tokens.get(token.token_id)
            if current is None:
                return
            if current.state is not TokenState.RETURN_PENDING:
                # 已被退役步骤转为 RETIRED，或已归还：空操作
                return
            counters = self._counters[current.generation]
            if counters.retired:
                # 旧代已退役：回调看见 RETIRED 后空操作，绝不访问新代 semaphore
                current.state = TokenState.RETIRED
                counters.Q -= 1
                counters.X += 1
                self._tokens.pop(token.token_id, None)
                return
            current.state = TokenState.RETURNED
            counters.Q -= 1
            counters.R += 1
            semaphore = current.semaphore
            self._tokens.pop(token.token_id, None)

        # 锁外 release（此处在属主 loop 线程内，asyncio.Semaphore 安全）
        semaphore.release()

    # ------------------------------------------------------------ future 绑定

    def attach(self, real_future: Any, token: SlotToken) -> None:
        """把结算挂到**真实任务**的 future 上（不是 asyncio 包装 future）。

        ``concurrent.futures.Future.add_done_callback`` 在 future 完成时执行，
        因此「超时响应早已返回、线程后来终于跑完」时才会结算。
        """

        def _on_done(_fut: Any) -> None:
            self.settle(token)

        real_future.add_done_callback(_on_done)

    # ------------------------------------------------------------ 等待者登记

    def register_waiter(self, task: asyncio.Future) -> int:
        """登记一个正在等待 acquire 的任务（``_acquire_slot_or_fastfail`` 的
        ``ensure_future`` acquire 任务）。

        返回 waiter id；调用方必须在退出路径（取得 / 取消 / 超时）``unregister_waiter``。
        登记表是 B20 新代创建条件 4「等待者全部取消或结算」的可观察面。
        """
        with self._lock:
            waiter_id = self._next_waiter_id
            self._next_waiter_id += 1
            self._waiters[waiter_id] = (task.get_loop(), task)
            return waiter_id

    def unregister_waiter(self, waiter_id: int) -> None:
        with self._lock:
            self._waiters.pop(waiter_id, None)
            self._waiters_cancel_requested.discard(waiter_id)

    def waiter_cancel_requested(self, waiter_id: int) -> bool:
        """该等待者的取消是否来自代际关闭（而非调用方自身被取消）。"""
        with self._lock:
            return waiter_id in self._waiters_cancel_requested

    def cancel_waiters(self) -> int:
        """代际关闭步骤：取消**当前代**全部已登记 acquire 等待者。

        DESIGN C1 §5 规则 2：等待者统一取消、按「传输关闭 / TOOL_BUSY」失败；
        **绝不**把等待者重新排入新代信号量（迁移 = 两代容量叠加 = 容量翻倍）。

        取消经 ``loop.call_soon_threadsafe(task.cancel)`` 投递（任意线程可调）；
        loop 已关闭的等待者随 loop 消亡，跳过即可。返回成功投递取消的个数。
        """
        with self._lock:
            counters = self._counters[self._generation]
            if not counters.closing:
                raise RuntimeError(
                    f"容量池 {self.name}：cancel_waiters 只能在 begin_close 之后调用"
                )
            entries = list(self._waiters.items())
            self._waiters_cancel_requested.update(wid for wid, _ in entries)

        delivered = 0
        for _waiter_id, (loop, task) in entries:
            try:
                loop.call_soon_threadsafe(task.cancel)
                delivered += 1
            except RuntimeError:
                # loop 已关闭：等待者随之消亡，无需取消
                pass
        return delivered

    # ---------------------------------------------------------------- 代际

    def begin_close(self) -> list[SlotToken]:
        """代际关闭第一步：置 closing，返回当前活动 token 快照。

        调用方须在锁外推进真实终止；closing 之后不得再接纳新调用。
        """
        with self._lock:
            self._counters[self._generation].closing = True
            return list(self._tokens.values())

    def retire(self) -> bool:
        """原子退役：仅把已结算的 RETURN_PENDING 转为 RETIRED。

        返回 ``True`` 表示退役完成（``P == 0`` 且 ``Q == 0``）。
        仍有真实执行的 ACTIVE 时**返回 False 且不改动任何账目**——禁止强制归零。
        """
        with self._lock:
            gen = self._generation
            counters = self._counters[gen]
            if counters.P != 0:
                # 不得把仍有真实执行的 ACTIVE 清零
                return False
            for token in list(self._tokens.values()):
                if token.generation != gen:
                    continue
                if token.state is TokenState.RETURN_PENDING:
                    token.state = TokenState.RETIRED
                    counters.Q -= 1
                    counters.X += 1
                    self._tokens.pop(token.token_id, None)
                elif token.state is TokenState.ACTIVE:
                    # 兜底：P 已为 0 时不应存在 ACTIVE
                    return False
            counters.retired = True
            counters.closing = True
            if counters.Q != 0:
                return False
            return True

    def can_rebuild(self) -> bool:
        """新代创建的五项条件（DESIGN C1 §5.1）。"""
        with self._lock:
            counters = self._counters[self._generation]
            if not counters.closing:
                return False
            if counters.P != 0 or counters.Q != 0:
                return False
            # 条件 4 的可观察面：等待者必须全部取消或结算（注销），
            # 仍有登记中的 acquire 等待者时不得换代——它们还可能取得旧代许可
            if self._waiters:
                return False
            return True

    def start_new_generation(self) -> int:
        """在满足五项条件后创建新一代，返回新代际 id。

        未满足条件时抛 ``RuntimeError``——**不得靠「强制归零」创建新代**。
        """
        if not self.can_rebuild():
            c = self.counters()
            raise RuntimeError(
                f"容量池 {self.name} 尚不满足新代创建条件：{c.as_dict()}"
            )
        with self._lock:
            self._generation += 1
            self._semaphore = asyncio.Semaphore(self.capacity)
            self._counters[self._generation] = GenerationCounters(
                generation=self._generation
            )
            return self._generation


class ExecutorLifecycle:
    """具名容量池的注册表（替代模块级裸 Semaphore，B20）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pools: dict[str, SlotPool] = {}

    def pool(self, name: str, capacity: int) -> SlotPool:
        with self._lock:
            existing = self._pools.get(name)
            if existing is None:
                existing = SlotPool(name, capacity)
                self._pools[name] = existing
            return existing

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._pools)

    def snapshot(self) -> dict[str, Any]:
        return {n: p.snapshot() for n, p in self._pools.items()}

    def assert_conservation(self) -> None:
        for pool in self._pools.values():
            pool.assert_conservation()


lifecycle = ExecutorLifecycle()


def make_done_callback(pool: SlotPool, token: SlotToken) -> Callable[[Any], None]:
    """返回可直接交给 ``add_done_callback`` 的结算闭包。"""

    def _cb(_fut: Any) -> None:
        pool.settle(token)

    return _cb

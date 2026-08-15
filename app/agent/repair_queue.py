"""AI Debug Agent 异步修复队列 —— 消息队列削峰。

结构对称 app.llm.analysis_queue.AnalysisQueue：
- 有界 asyncio.Queue(maxsize=N) + asyncio.Semaphore(K) + K 常驻消费协程
- 满载时新请求直接 429（快速失败）
- 消费协程内调 Coordinator.run（延迟导入破循环依赖）

与 AnalysisQueue 解耦：独立 workers 配额，避免修复任务抢占分析任务的 LLM RPM。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("lujo-mcp.agent.queue")


class QueueFullError(Exception):
    """队列已满（峰容量 N 达到），背压拒绝入队。"""


class RepairQueue:
    """有界队列 + K 常驻消费协程的修复任务队列。"""

    # FIX: P1-3 _jobs 只增不减，enqueue 时对超过 TTL 的终态记录做清理
    _JOB_TTL_SECONDS = 3600
    _JOB_MAX_ENTRIES = 1000

    def __init__(self, maxsize: int, concurrency: int) -> None:
        self._maxsize = maxsize
        self._concurrency = concurrency
        self._queue: asyncio.Queue[tuple[str, dict, Optional[str]]] = asyncio.Queue(
            maxsize=maxsize
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        # 任务状态存储：job_id -> {status, result, error, created_at, finished_at}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = asyncio.Lock()
        self._workers: list[asyncio.Task] = []

    async def enqueue(
        self, context: dict, model: Optional[str] = None
    ) -> str:
        """入队一个修复任务。队列满则抛 QueueFullError 立即背压。"""
        job_id = str(uuid.uuid4())
        async with self._jobs_lock:
            self._jobs[job_id] = {
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": time.time(),
                "finished_at": None,
            }
            # FIX: P1-3 终态记录 TTL 清理，防止 _jobs 无限增长
            self._evict_stale_jobs_locked()
        try:
            self._queue.put_nowait((job_id, context, model))
        except asyncio.QueueFull:
            # 回滚已登记的 job，避免状态泄漏
            async with self._jobs_lock:
                self._jobs.pop(job_id, None)
            raise QueueFullError(job_id)
        return job_id

    def _evict_stale_jobs_locked(self) -> None:
        """清理超时且已终态（done/failed/rejected）的 job 记录（须持 _jobs_lock）。"""
        if len(self._jobs) <= self._JOB_MAX_ENTRIES:
            return
        cutoff = time.time() - self._JOB_TTL_SECONDS
        stale = [
            jid
            for jid, job in self._jobs.items()
            if job["status"] in ("done", "failed", "rejected")
            and (job.get("finished_at") or 0) < cutoff
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    async def _worker(self) -> None:
        """常驻消费协程：从队列取任务 → 信号量限并发 → 调 Coordinator.run。

        延迟导入 Coordinator 规避循环依赖
        （coordinator → repair_agent → analyzer → ...）。
        """
        from app.agent.coordinator import Coordinator

        try:
            while True:
                job_id, context, model = await self._queue.get()
                try:
                    async with self._jobs_lock:
                        if job_id in self._jobs:
                            self._jobs[job_id]["status"] = "running"
                    async with self._semaphore:
                        result = await Coordinator().run(context, model)
                    async with self._jobs_lock:
                        if job_id in self._jobs:
                            self._jobs[job_id]["status"] = "done"
                            self._jobs[job_id]["result"] = result
                            self._jobs[job_id]["finished_at"] = time.time()
                except asyncio.CancelledError:
                    # FIX: P1-3 worker 被取消时，先把 in-flight job 标记 failed，
                    # 再 re-raise，避免 _jobs 永久卡 running
                    async with self._jobs_lock:
                        if job_id in self._jobs and self._jobs[job_id]["status"] == "running":
                            self._jobs[job_id]["status"] = "failed"
                            self._jobs[job_id]["error"] = "cancelled (drain timeout)"
                            self._jobs[job_id]["finished_at"] = time.time()
                    raise
                except Exception as e:
                    logger.exception("repair job %s failed", job_id)
                    async with self._jobs_lock:
                        if job_id in self._jobs:
                            self._jobs[job_id]["status"] = "failed"
                            self._jobs[job_id]["error"] = str(e)
                            self._jobs[job_id]["finished_at"] = time.time()
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            # 优雅退出
            return

    async def start(self, n_workers: int) -> None:
        """启动 n_workers 个常驻消费协程。重复调用幂等（追加不重叠）。"""
        for _ in range(n_workers):
            self._workers.append(asyncio.create_task(self._worker()))

    async def drain(self, timeout: float) -> dict[str, int]:
        """优雅停机：先让 worker 排空队列，超时后强制取消。

        修复：先等待队列排空（worker 仍在消费），超时后再取消 worker。
        原实现先取消 worker 再 join()，但 worker 退出后无人调 task_done()，
        导致 join() 永远超时。
        """
        unfinished = 0
        drained = 0

        # 先尝试让队列自然排空（worker 仍在消费）
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            # FIX: P1-3 排空超时：清空队列残留并标记 rejected，
            # 否则这些 item 永远无人消费，后续 enqueue 的 job 也永不执行
            rejected = 0
            while True:
                try:
                    job_id, _ctx, _model = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                async with self._jobs_lock:
                    if job_id in self._jobs and self._jobs[job_id]["status"] == "pending":
                        self._jobs[job_id]["status"] = "rejected"
                        self._jobs[job_id]["error"] = "rejected (drain timeout)"
                        self._jobs[job_id]["finished_at"] = time.time()
                        rejected += 1
                self._queue.task_done()
            unfinished = rejected
            logger.warning(
                "repair queue drain timed out: %d jobs rejected, cancelling workers",
                unfinished,
            )

        # 取消所有 worker（如果还没退出）
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers.clear()

        # 统计本周期内已完成 job 数（done / failed）
        async with self._jobs_lock:
            for job in self._jobs.values():
                if job["status"] in ("done", "failed"):
                    drained += 1
        return {"drained": drained, "unfinished": unfinished}

    def get_job(self, job_id: str) -> Optional[dict]:
        """返回任务状态副本；未知 job_id 返回 None。

        单线程事件循环下 dict 读为原子操作，sync 方法无需持锁。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return copy.deepcopy(job)

    def queue_size(self) -> int:
        """当前队列中待消费任务数。"""
        return self._queue.qsize()


# ── 模块级单例与 lifespan helper ──
_repair_queue: Optional[RepairQueue] = None


def get_repair_queue() -> RepairQueue:
    """惰性初始化全局队列单例，参数从 settings 读取。"""
    global _repair_queue
    if _repair_queue is None:
        from app.config import settings

        _repair_queue = RepairQueue(
            maxsize=settings.agent_queue_maxsize,
            concurrency=settings.agent_queue_workers,
        )
    return _repair_queue


async def start_repair_queue() -> None:
    """lifespan 启动钩子：按 settings.agent_queue_workers 启动消费协程。"""
    from app.config import settings

    q = get_repair_queue()
    await q.start(settings.agent_queue_workers)
    logger.info(
        "repair queue started: maxsize=%d workers=%d",
        settings.agent_queue_maxsize,
        settings.agent_queue_workers,
    )


async def drain_repair_queue(timeout: float) -> dict[str, int]:
    """lifespan 关闭钩子：排空队列并返回 drain 统计。"""
    global _repair_queue
    if _repair_queue is None:
        return {"drained": 0, "unfinished": 0}
    stats = await _repair_queue.drain(timeout=timeout)
    logger.info("repair queue drained: %s", stats)
    return stats

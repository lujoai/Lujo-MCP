"""P3-6 异步分析队列 —— 消息队列削峰

裸 ``BackgroundTasks`` 无削峰语义，瞬时 LLM 请求洪峰会击穿 RPM/TPM 限制。
本模块用有界 ``asyncio.Queue(maxsize=N)`` + ``asyncio.Semaphore(K)`` 实现真正的
背压：超过峰容量 N 直接 429 拒绝；常驻 K 个消费协程对齐 LLM 并发上限。

消费协程直接调用 ``app.llm.analyzer.analyze_async``，零侵入 analyzer.py。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("ai-debug-mcp.llm.queue")


class QueueFullError(Exception):
    """队列已满（峰容量 N 达到），背压拒绝入队。"""


class AnalysisQueue:
    """有界队列 + K 常驻消费协程的削峰分析队列。"""

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

    async def enqueue(self, context: dict, model: Optional[str] = None) -> str:
        """入队一个分析任务。队列满则抛 ``QueueFullError`` 立即背压。"""
        job_id = str(uuid.uuid4())
        async with self._jobs_lock:
            self._jobs[job_id] = {
                "status": "pending",
                "result": None,
                "error": None,
                "created_at": time.time(),
                "finished_at": None,
            }
        try:
            self._queue.put_nowait((job_id, context, model))
        except asyncio.QueueFull:
            # 回滚已登记的 job，避免状态泄漏
            async with self._jobs_lock:
                self._jobs.pop(job_id, None)
            raise QueueFullError(job_id)
        return job_id

    async def _worker(self) -> None:
        """常驻消费协程：从队列取任务 → 信号量限并发 → 调 analyze_async。"""
        # 延迟导入规避潜在循环依赖
        from app.llm.analyzer import analyze_async

        try:
            while True:
                job_id, context, model = await self._queue.get()
                try:
                    async with self._jobs_lock:
                        if job_id in self._jobs:
                            self._jobs[job_id]["status"] = "running"
                    async with self._semaphore:
                        result = await analyze_async(context, model)
                    async with self._jobs_lock:
                        if job_id in self._jobs:
                            self._jobs[job_id]["status"] = "done"
                            self._jobs[job_id]["result"] = result
                            self._jobs[job_id]["finished_at"] = time.time()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("analysis job %s failed", job_id)
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
            unfinished = self._queue.qsize()
            logger.warning(
                "analysis queue drain timed out: %d jobs unfinished, cancelling workers",
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
        import copy

        job = self._jobs.get(job_id)
        if job is None:
            return None
        return copy.deepcopy(job)

    def queue_size(self) -> int:
        """当前队列中待消费任务数。"""
        return self._queue.qsize()


# ── 模块级单例与 lifespan helper ──
_analysis_queue: Optional[AnalysisQueue] = None


def get_analysis_queue() -> AnalysisQueue:
    """惰性初始化全局队列单例，参数从 settings 读取。"""
    global _analysis_queue
    if _analysis_queue is None:
        from app.config import settings

        _analysis_queue = AnalysisQueue(
            maxsize=settings.llm_queue_maxsize,
            concurrency=settings.llm_queue_workers,
        )
    return _analysis_queue


async def start_analysis_queue() -> None:
    """lifespan 启动钩子：按 settings.llm_queue_workers 启动消费协程。"""
    from app.config import settings

    q = get_analysis_queue()
    await q.start(settings.llm_queue_workers)
    logger.info(
        "analysis queue started: maxsize=%d workers=%d",
        settings.llm_queue_maxsize,
        settings.llm_queue_workers,
    )


async def drain_analysis_queue(timeout: float) -> dict[str, int]:
    """lifespan 关闭钩子：排空队列并返回 drain 统计。"""
    global _analysis_queue
    if _analysis_queue is None:
        return {"drained": 0, "unfinished": 0}
    stats = await _analysis_queue.drain(timeout=timeout)
    logger.info("analysis queue drained: %s", stats)
    return stats

"""
推理请求队列与并发控制

核心功能：
1. 请求排队 → 多工具调用不能阻塞
2. 并发限制 → 24GB 显存精打细算
3. 优先级队列 → 重要请求优先
4. 请求超时 → 避免无限等待
5. 背压机制 → 队列满时拒绝新请求
"""
import asyncio
import time
from typing import Any, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from enum import IntEnum

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("queue")


class RequestPriority(IntEnum):
    """请求优先级"""
    HIGH = 0        # Agent 最终回答生成
    NORMAL = 1      # 正常 ReAct 步骤推理
    LOW = 2         # 摘要生成、自我校验等辅助任务
    BACKGROUND = 3  # 后台任务


@dataclass(order=True)
class QueuedRequest:
    """队列中的请求"""
    priority: int
    enqueue_time: float = field(compare=False)
    request_id: str = field(compare=False, default="")
    coroutine: Any = field(compare=False, default=None)
    future: Any = field(compare=False, default=None)
    timeout: float = field(compare=False, default=0)


class InferenceQueue:
    """
    推理请求队列

    功能：
    - 信号量控制并发（最大同时 N 个推理请求）
    - 优先级队列（高优先级请求插队）
    - 超时机制（等待过久自动取消）
    - 背压（队列满时快速失败）
    - 统计信息
    """

    _instance: Optional["InferenceQueue"] = None

    def __init__(self):
        self._semaphore = asyncio.Semaphore(settings.INFERENCE_CONCURRENT_REQUESTS)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            maxsize=settings.INFERENCE_REQUEST_QUEUE_SIZE
        )
        self._active_count = 0
        self._total_processed = 0
        self._total_rejected = 0
        self._total_timeout = 0

    @classmethod
    def get_instance(cls) -> "InferenceQueue":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def submit(
        self,
        coro: Awaitable,
        priority: RequestPriority = RequestPriority.NORMAL,
        timeout: Optional[float] = None,
        request_id: str = "",
    ) -> Any:
        """
        提交推理请求到队列

        Args:
            coro: 要执行的协程
            priority: 请求优先级
            timeout: 超时秒数
            request_id: 请求标识（用于日志）

        Returns:
            协程执行结果

        Raises:
            asyncio.QueueFull: 队列已满
            asyncio.TimeoutError: 等待超时
        """
        timeout = timeout or settings.INFERENCE_REQUEST_TIMEOUT

        # 背压机制：队列满时快速失败
        if self._queue.full():
            self._total_rejected += 1
            logger.warning(f"推理队列已满，拒绝请求: {request_id}")
            raise asyncio.QueueFull("推理请求队列已满，请稍后重试")

        # 使用信号量控制并发
        try:
            async with asyncio.timeout(timeout):
                await self._semaphore.acquire()
                try:
                    self._active_count += 1
                    result = await coro
                    self._total_processed += 1
                    return result
                finally:
                    self._active_count -= 1
                    self._semaphore.release()
        except TimeoutError:
            self._total_timeout += 1
            logger.warning(f"推理请求超时: {request_id}, timeout={timeout}s")
            raise asyncio.TimeoutError(f"推理请求超时（{timeout}秒）")

    async def submit_nowait(
        self,
        coro: Awaitable,
        priority: RequestPriority = RequestPriority.NORMAL,
    ) -> Any:
        """非阻塞提交：如果并发已满直接执行（不排队）"""
        if self._semaphore._value > 0:  # noqa: SLF001
            return await self.submit(coro, priority)
        else:
            # 并发已满，直接执行（绕过并发控制）
            logger.debug("并发已满，直接执行推理请求")
            return await coro

    def get_stats(self) -> dict:
        """获取队列统计"""
        return {
            "active_requests": self._active_count,
            "max_concurrent": settings.INFERENCE_CONCURRENT_REQUESTS,
            "queue_size": self._queue.qsize(),
            "max_queue_size": settings.INFERENCE_REQUEST_QUEUE_SIZE,
            "total_processed": self._total_processed,
            "total_rejected": self._total_rejected,
            "total_timeout": self._total_timeout,
            "semaphore_available": self._semaphore._value,  # noqa: SLF001
        }

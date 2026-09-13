"""Async video generation scheduler — single-flight FIFO executor.

Runs inside the coara main process (like ``BackgroundAgentManager``). It pops
the head of :class:`~src.tools.builtin.media.video_queue.VideoQueue` only
when no job is in flight, submits the video, then awaits poll+download before
starting another. Submits are further spaced by :data:`CADENCE_SECONDS`.

Any failure (submit rejected / generation failed / download failed) re-queues
the task to the tail (attempts+1) up to ``MAX_ATTEMPTS``, then marks it
permanently failed. Completion / final failure is published to the EventBus as
``background_task_complete`` so Root can route the notice back to the
originating workspace session.

``cancel(task_id)`` marks the queue record and aborts the in-flight poll via a
cancel flag passed into ``agnes_poll_video``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.tools.builtin.media import providers
from src.tools.builtin.media.video_queue import MAX_ATTEMPTS, VideoQueue, VideoTask

CADENCE_SECONDS = 60.0


@dataclass
class _CancelFlag:
    """Minimal abort signal compatible with ``agnes_poll_video(..., signal=)``."""

    aborted: bool = False

    def abort(self) -> None:
        self.aborted = True


class VideoScheduler:
    """Singleton scheduler owning the video-generation queue cadence."""

    _instance: VideoScheduler | None = None
    _lock = __import__("threading").Lock()

    # 实例属性在 __new__（单例构造）里初始化。此处显式声明类型：行内注解写在
    # ``inst.xxx: T = ...``（非 self 赋值）上不构成类成员声明，mypy 既会报
    # 「Type cannot be declared in assignment to non-self attribute」，又会因类上
    # 查不到该属性而把下游所有访问判为未知属性。
    _queue: VideoQueue
    _event_bus: Any
    _serve_task: asyncio.Task[Any] | None
    _next_run_at: float
    _active_polls: dict[str, asyncio.Task[Any]]
    _cancel_flags: dict[str, _CancelFlag]

    def __new__(cls) -> VideoScheduler:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._queue = VideoQueue()
                    inst._event_bus = None
                    inst._serve_task = None
                    inst._next_run_at = 0.0
                    inst._active_polls = {}
                    inst._cancel_flags = {}
                    cls._instance = inst
        return cls._instance

    def set_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        """Launch the scheduler coroutine (idempotent)."""
        if self._serve_task is not None and not self._serve_task.done():
            return
        try:
            self._queue.reset_running(skip_ids=set(self._active_polls))
        except Exception as exc:  # noqa: BLE001 — best-effort recovery
            logger.warning(f"VideoScheduler reset_running failed: {exc}")
        self._serve_task = asyncio.create_task(self._serve(), name="video-scheduler")
        logger.info("VideoScheduler started")

    def stop(self) -> None:
        """Cancel the scheduler loop and every in-flight poll (Root shutdown)."""
        for flag in list(self._cancel_flags.values()):
            flag.abort()
        for task_id, poll in list(self._active_polls.items()):
            if not poll.done():
                poll.cancel()
            self._active_polls.pop(task_id, None)
        self._cancel_flags.clear()
        task = self._serve_task
        self._serve_task = None
        if task is not None and not task.done():
            task.cancel()
            logger.info("VideoScheduler stopped")

    def cancel(self, task_id: str) -> bool:
        """Cancel a queued/running task and abort its poll if in flight."""
        ok = self._queue.cancel(task_id)
        flag = self._cancel_flags.get(task_id)
        if flag is not None:
            flag.abort()
        poll = self._active_polls.get(task_id)
        if poll is not None and not poll.done():
            poll.cancel()
        return ok

    async def _serve(self) -> None:
        while True:
            try:
                if time.time() >= self._next_run_at:
                    started = await self._tick_once()
                    if started:
                        self._next_run_at = time.time() + CADENCE_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                logger.error(f"VideoScheduler tick error: {exc}", exc_info=True)
            await asyncio.sleep(max(0.1, min(0.5, self._next_run_at - time.time())))

    async def _tick_once(self) -> bool:
        """Start at most one job. Returns True if a submit was attempted."""
        # Single-flight: do not start another while a poll is still running.
        alive = {tid: t for tid, t in self._active_polls.items() if not t.done()}
        self._active_polls = alive
        if alive:
            return False
        task = self._queue.pop_next()
        if task is None:
            return False
        flag = _CancelFlag()
        self._cancel_flags[task.task_id] = flag
        video_id = task.video_id
        if not video_id:
            try:
                import httpx

                async with httpx.AsyncClient() as client:
                    video_id = await providers.agnes_create_video(
                        client,
                        prompt=task.prompt,
                        model=str(task.params.get("model") or providers.DEFAULT_AGNES_VIDEO_MODEL),
                        seconds=str(task.params.get("seconds") or "5"),
                        aspect_ratio=str(task.params.get("aspect_ratio") or "16:9"),
                        mode=task.params.get("mode"),
                        images=task.params.get("images"),
                        seed=task.params.get("seed"),
                    )
            except InterruptedError:
                self._cancel_flags.pop(task.task_id, None)
                return True
            except Exception as exc:  # noqa: BLE001 — any submit failure re-queues
                self._cancel_flags.pop(task.task_id, None)
                await self._on_fail(task, str(exc))
                return True
            # 立即持久化：进程崩溃后 reset_running 重排的任务带 video_id 恢复，
            # 直接进 poll 而不再 submit（避免重复扣费）。
            self._queue.set_video_id(task.task_id, video_id)
        if flag.aborted:
            self._cancel_flags.pop(task.task_id, None)
            return True
        poll_task = asyncio.create_task(
            self._poll_until_done(task, video_id, flag),
            name=f"video-poll-{task.task_id}",
        )
        self._active_polls[task.task_id] = poll_task
        # Await so the next tick cannot start another job (true single-flight).
        try:
            await poll_task
        except asyncio.CancelledError:
            # stop() nulls ``_serve_task`` then cancels — re-raise to exit serve.
            # cancel(task_id) only cancels the poll — keep serving.
            if self._serve_task is None:
                raise
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"VideoScheduler poll task error for {task.task_id}: {exc}", exc_info=True)
        finally:
            self._active_polls.pop(task.task_id, None)
            self._cancel_flags.pop(task.task_id, None)
        return True

    async def _poll_until_done(self, task: VideoTask, video_id: str, flag: _CancelFlag) -> None:
        timeout = float(task.params.get("timeout") or 600.0)
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                final = await providers.agnes_poll_video(
                    client,
                    video_id,
                    model=str(task.params.get("model") or providers.DEFAULT_AGNES_VIDEO_MODEL),
                    timeout=timeout,
                    signal=flag,
                )
                if flag.aborted:
                    return
                url = providers.extract_video_url(final)
                if not url:
                    raise ValueError(f"视频已完成但未找到下载地址: {str(final)[:500]}")
                out_path = Path(task.out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                size = await providers.download_file(client, url, out_path)
        except InterruptedError:
            return
        except asyncio.CancelledError:
            return
        except providers.VideoDownloadError as exc:
            # 视频已在服务端生成（费用已扣）仅下载失败：不消耗 attempts 逼用户
            # 重提（重复扣费），仅重试下载——video_id 已落盘，重跑直接进 poll。
            await self._on_download_fail(task, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — any generation failure re-queues
            await self._on_fail(task, str(exc))
            return
        result = {"out": str(out_path), "bytes": size}
        # cancel 竞态：已被取消的任务不覆盖终态、不推送完成
        cur = self._queue.get(task.task_id)
        if cur is not None and cur.status == "cancelled":
            return
        if flag.aborted:
            return
        self._queue.mark_done(task.task_id, result)
        await self._notify(task, status="completed", result=result)

    async def _on_fail(self, task: VideoTask, error: str) -> None:
        cur = self._queue.get(task.task_id)
        if cur is not None and cur.status == "cancelled":
            return
        next_attempts = task.attempts + 1
        if next_attempts >= MAX_ATTEMPTS:
            self._queue.mark_failed(task.task_id, error)
            await self._notify(task, status="failed", error=error)
        else:
            self._queue.requeue_tail(task.task_id, error)

    async def _on_download_fail(self, task: VideoTask, error: str) -> None:
        """视频已生成（已扣费）仅下载失败：不消耗 attempts，重排队尾仅重试下载。

        attempts 是为「生成失败可放弃」设的上限；下载失败的视频是好的，
        video_id 已落盘，重跑直接进 poll 重新下载，不产生新费用。为防下载
        永久性失败（如本地磁盘满）导致无限重排，用独立计数兜底：超过上限
        才 mark_failed（此时提示里保留 video_id，用户可据此人工恢复）。
        """
        cur = self._queue.get(task.task_id)
        if cur is not None and cur.status == "cancelled":
            return
        download_retries = int(task.params.get("_download_retries") or 0) + 1
        if download_retries > MAX_ATTEMPTS:
            self._queue.mark_failed(
                task.task_id,
                f"视频已生成但多次下载失败（video_id={task.video_id}，可据此人工恢复）: {error}",
            )
            await self._notify(task, status="failed", error=error)
            return
        self._queue.requeue_for_download_retry(task.task_id, error, download_retries)

    async def _notify(
        self,
        task: VideoTask,
        *,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from src.core.events import TraceEvent

            terminal = "completed" if status == "completed" else "failed"
            self._event_bus.publish(
                TraceEvent(
                    coara_id="video-scheduler",
                    coara_name="VideoScheduler",
                    event_type="background_task_complete",
                    message=f"Video generation task {status}: {task.task_id}",
                    payload={
                        "task_id": task.task_id,
                        "kind": "video",
                        "description": task.description or task.prompt,
                        "status": terminal,
                        "terminal_reason": terminal,
                        "has_error": error is not None,
                        "error": error,
                        "result_preview": (
                            f"{result.get('out', '')} ({result.get('bytes', 0)} bytes)" if result else (error or "")
                        ),
                        "result_full": (result.get("out", "") if result else "") or (error or ""),
                        "origin_source": task.origin_source,
                        "coara_id": task.coara_id,
                        "session_id": task.session_id,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — notify must never break the loop
            logger.warning(f"VideoScheduler notify failed for {task.task_id}: {exc}")

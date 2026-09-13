"""Wake scanner for suspended workflow instances."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from wdl.logging import logger
from wdl.persistence import WorkflowPersistence

ResumeCallback = Callable[[str, dict[str, Any] | None], Any]


class WorkflowWakeScanner:
    """Periodically scan for due waiting instances and resume them."""

    def __init__(
        self,
        persistence: WorkflowPersistence,
        resume_callback: ResumeCallback,
        interval_seconds: float | None = None,
    ):
        if interval_seconds is None:
            import os

            interval_seconds = float(os.environ.get("COARA_WAKE_SCAN_INTERVAL", "5.0"))
        self.persistence = persistence
        self.resume_callback = resume_callback
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False
        self._pending_resumes: set[asyncio.Task] = set()

    def start(self) -> None:
        """Start the background scanner."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("Workflow wake scanner started")

    def stop(self) -> None:
        """Stop the scanner."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        for task in list(self._pending_resumes):
            if not task.done():
                task.cancel()

    async def _scan_loop(self) -> None:
        """Main scan loop."""
        while self._running:
            try:
                await self._scan_once()
            except Exception as e:
                logger.error(f"Wake scanner error: {e}")
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    async def _scan_once(self) -> None:
        """Scan for due instances and resume them."""
        due_ids = await self.persistence.find_due_instances()
        for instance_id in due_ids:
            logger.info(f"Resuming workflow instance: {instance_id}")
            try:
                task = asyncio.create_task(self.resume_callback(instance_id, None))
                self._pending_resumes.add(task)
                task.add_done_callback(self._on_resume_done)
            except Exception as e:
                logger.error(f"Failed to resume {instance_id}: {e}")

        timed_out_events = await self.persistence.find_timed_out_event_instances()
        for item in timed_out_events:
            instance_id = item["instance_id"]
            event_type = item.get("wait_event")
            logger.info(f"Resuming timed out event wait: {instance_id} ({event_type})")
            try:
                task = asyncio.create_task(
                    self.resume_callback(
                        instance_id,
                        {
                            "event_type": event_type,
                            "timed_out": True,
                        },
                    )
                )
                self._pending_resumes.add(task)
                task.add_done_callback(self._on_resume_done)
            except Exception as e:
                logger.error(f"Failed to resume timed out event wait {instance_id}: {e}")

    def _on_resume_done(self, task: asyncio.Task) -> None:
        """Discard finished resume tasks; log failures."""
        self._pending_resumes.discard(task)
        if not task.cancelled() and task.exception():
            logger.warning(f"Workflow resume failed: {task.exception()}")

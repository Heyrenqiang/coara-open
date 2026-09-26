"""Background Agent Manager — asyncio.Task lifecycle for background subagents."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.coara.injections.background_injector import clip_result_for_persist
from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


def _same_workspace_dir(current: Any, launch: str) -> bool:
    """Resolved-path workspace equality (separators/case); OSError → raw str compare."""
    try:
        return Path(current).resolve() == Path(launch).resolve()
    except OSError:
        return str(current) == launch


class BackgroundAgentManager:
    """Singleton: track background subagent asyncio.Tasks (not persistence)."""

    _instance: BackgroundAgentManager | None = None
    _singleton_lock = threading.Lock()

    _tasks: dict[str, asyncio.Task | None]
    _cancel_requested: set[str]
    _event_bus: Any

    def __new__(cls) -> BackgroundAgentManager:
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._tasks = {}
                    instance._cancel_requested = set()
                    instance._task_workspaces = {}
                    instance._task_origins = {}
                    instance._event_bus = None
                    cls._instance = instance
        return cls._instance

    def set_event_bus(self, event_bus: Any) -> None:
        """Wire Root EventBus so completions publish (session CoaraBase has none)."""
        self._event_bus = event_bus

    async def start(
        self,
        parent_coara: CoaraBase,
        coro: Callable[[], Any],
        *,
        task_id: str,
        subagent_type: str,
        description: str,
        child_coara_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> str:
        """Start background subagent; return task_id immediately."""
        self._tasks[task_id] = None  # placeholder before task starts
        launch_workspace_dir = str(parent_coara.workspace_dir)
        self._task_workspaces[task_id] = launch_workspace_dir
        from src.coara.turn_source import current_turn_source

        origin_source = current_turn_source(parent_coara)
        self._task_origins[task_id] = origin_source
        _launch_wm = getattr(parent_coara, "workspace_manager", None)
        launch_coara_home = getattr(_launch_wm, "coara_home", None) if _launch_wm else None

        # TaskStore initial write is DelegateTool.execute()'s job (avoid dup)

        # Ledger: restart can surface interrupted subagent tasks
        from src.coara.subagent_task_ledger import record_started

        record_started(
            launch_workspace_dir,
            launch_coara_home,
            task_id=task_id,
            subagent_type=subagent_type,
            description=description,
            session_id=str(getattr(parent_coara, "session_id", "") or ""),
        )

        async def _wrapped() -> Any:
            """Run coro; notify + cleanup in finally."""
            result: Any = None
            error: str | None = None
            try:
                result = await coro()
            except asyncio.CancelledError:
                error = "cancelled"
                logger.warning(f"Background agent [{task_id}] was cancelled")
                raise
            except Exception as exc:
                error = str(exc)
                logger.error(f"Background agent [{task_id}] failed: {exc}")
            finally:
                self._tasks.pop(task_id, None)
                self._task_workspaces.pop(task_id, None)
                self._task_origins.pop(task_id, None)
                self._cancel_requested.discard(task_id)

                # Full result for inject + TaskStore (clip oversize)
                result_full = ""
                if hasattr(result, "content"):
                    result_full = str(result.content) if isinstance(result.content, str) else repr(result.content)
                elif isinstance(result, str):
                    result_full = result
                result_full = clip_result_for_persist(result_full)
                result_preview = result_full[:300]

                is_cancelled = error == "cancelled" or (hasattr(result, "is_cancelled") and result.is_cancelled)

                if is_cancelled:
                    event_status = "killed"
                    terminal_reason = "cancelled"
                elif error:
                    event_status = "failed"
                    terminal_reason = "failed"
                else:
                    event_status = "completed"
                    terminal_reason = "completed"

                if parent_coara is not None:
                    # Segment origin from delegate metadata (same anchor as diff)
                    _sub_origin = ""
                    _sub_channel = ""
                    try:
                        _meta = getattr(result, "metadata", None) or {}
                        _sub_origin = str(_meta.get("subagent_origin") or "")
                        _sub_channel = str(_meta.get("subagent_origin_channel") or "")
                    except Exception:  # noqa: BLE001
                        _sub_origin = ""
                        _sub_channel = ""
                    parent_coara._emit_trace(
                        "background_agent_complete",
                        f"Background agent completed: {subagent_type}",
                        payload={
                            "task_id": task_id,
                            "subagent_type": subagent_type,
                            "description": description,
                            "has_error": error is not None and not is_cancelled,
                            "error": error,
                            "result_preview": result_preview[:300],
                            "child_coara_id": child_coara_id,
                            # Origin end: CLI banner shows only this end's tasks
                            "origin_source": origin_source,
                            "subagent_origin": _sub_origin,
                            "subagent_origin_channel": _sub_channel,
                            # Launch workspace snapshot (survives view switch)
                            "workspace_dir": launch_workspace_dir,
                        },
                    )

                same_project = parent_coara is not None and _same_workspace_dir(
                    parent_coara.workspace_dir, launch_workspace_dir
                )
                if not same_project and parent_coara is not None:
                    logger.info(
                        f"Background agent [{task_id}] completed in a different workspace "
                        f"(launched in {launch_workspace_dir}, now in {parent_coara.workspace_dir}); "
                        f"skipping EventBus injection to avoid cross-workspace pollution"
                    )
                event_bus = getattr(parent_coara, "event_bus", None) or self._event_bus
                if (
                    parent_coara is not None
                    and same_project
                    # janitor/daily: no parent continuation; aide/coaras must return to main
                    and subagent_type not in ("janitor", "daily")
                    and event_bus is not None
                ):
                    try:
                        from src.core.events import TraceEvent

                        event_bus.publish(
                            TraceEvent(
                                coara_id="background-agent-manager",
                                coara_name="BackgroundAgentManager",
                                event_type="background_task_complete",
                                message=f"Background agent task completed: {subagent_type}",
                                payload={
                                    "task_id": task_id,
                                    "kind": "agent",
                                    "subagent_type": subagent_type,
                                    "description": description,
                                    "status": event_status,
                                    "terminal_reason": terminal_reason,
                                    "has_error": error is not None and not is_cancelled,
                                    "error": error,
                                    "result_preview": result_preview,
                                    "result_full": result_full,
                                    "origin_source": origin_source,
                                    "subagent_origin": _sub_origin,
                                    "subagent_origin_channel": _sub_channel,
                                    "coara_id": str(getattr(parent_coara.identity, "coara_id", "") or ""),
                                    "session_id": str(getattr(parent_coara, "session_id", "") or ""),
                                },
                            )
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to publish agent task completion: {exc}")

                if parent_coara is not None:
                    try:
                        from src.background.task_store import TaskStatus as BgTaskStatus
                        from src.background.task_store_paths import task_store_for_workspace
                        from src.core.time import now_iso

                        ts = task_store_for_workspace(launch_workspace_dir, configured_home=launch_coara_home)
                        if terminal_reason == "completed":
                            persist_status = BgTaskStatus.COMPLETED.value
                        elif terminal_reason in {"cancelled", "killed"}:
                            persist_status = BgTaskStatus.KILLED.value
                        else:
                            persist_status = BgTaskStatus.FAILED.value
                        ts.update(
                            task_id,
                            status=persist_status,
                            error=error or "",
                            result_preview=result_preview,
                            result_full=result_full,
                            updated_at=now_iso(),
                            completed_at=now_iso(),
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to update TaskStore for {task_id}: {exc}")

                    # Ledger terminal: restart scan must not treat as interrupted
                    from src.coara.subagent_task_ledger import record_terminal

                    record_terminal(
                        launch_workspace_dir,
                        launch_coara_home,
                        task_id=task_id,
                        status="completed" if terminal_reason == "completed" else "failed",
                    )

            return result

        task = asyncio.create_task(_wrapped(), name=task_id)
        self._tasks[task_id] = task
        if task_id in self._cancel_requested:
            self._cancel_requested.discard(task_id)
            task.cancel()
            logger.info(f"Background agent [{task_id}] cancelled immediately (cancel requested before start)")

        if parent_coara is not None:
            start_payload: dict[str, Any] = {
                "task_id": task_id,
                "subagent_type": subagent_type,
                "description": description,
                "child_coara_id": child_coara_id,
            }
            if parent_tool_call_id:
                start_payload["parent_tool_call_id"] = parent_tool_call_id
                start_payload["parent_activity_id"] = parent_tool_call_id
            parent_coara._emit_trace(
                "background_agent_start",
                f"Background agent started: {subagent_type}",
                payload=start_payload,
            )

        logger.info(f"Background agent [{task_id}] started: {subagent_type} - {description}")
        return task_id

    async def cancel(self, task_id: str) -> bool:
        """Request cancel. Placeholder (None task) → queue cancel; TaskStore update is caller's job."""
        task = self._tasks.get(task_id)
        if task is None:
            if task_id in self._tasks:
                logger.debug(f"Background agent [{task_id}] cancel requested before its task started")
                self._cancel_requested.add(task_id)
            return False
        if task.done():
            return False
        task.cancel()
        return True

    def cancel_all(self, workspace_dir: str | None = None, origin_source: str | None = None) -> int:
        """Hard-cancel tasks. workspace_dir → that workspace only; origin_source → that end only; both None → all."""

        def _in_scope(tid: str) -> bool:
            if workspace_dir is not None:
                launch = self._task_workspaces.get(tid, "")
                if launch and not _same_workspace_dir(launch, str(workspace_dir)):
                    return False
            if origin_source is not None:
                origin = self._task_origins.get(tid, "")
                if origin and origin != str(origin_source):
                    return False
            return True

        cancelled = 0
        for task_id, task in list(self._tasks.items()):
            if not _in_scope(task_id):
                continue
            if task is None:
                self._cancel_requested.add(task_id)
                cancelled += 1
                logger.debug(f"Background agent [{task_id}] cancel-all before start")
                continue
            if task.done():
                continue
            task.cancel()
            cancelled += 1
        return cancelled

    async def wait_all(self, timeout: float = 3.0) -> None:
        """Await remaining tasks (after cancel_all); timeout abandons hangers."""
        tasks = [t for t in list(self._tasks.values()) if t is not None and not t.done()]
        if not tasks:
            return
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)

    def is_running(self, task_id: str) -> bool:
        """Return whether the given task_id is currently running."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        return not task.done()

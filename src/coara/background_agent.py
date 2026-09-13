"""Background Agent Manager — 管理后台运行的 subagent 任务。

只负责 asyncio.Task 的生命周期管理（启动、取消、清理）。
状态持久化统一由 TaskStore 负责。
task_id 与 subagent_id 统一（sa-xxx），便于 TaskStore 与后台完成通知关联。
"""

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
    """Compare workspace dirs by resolved path (separators/case normalized).

    Non-existent paths resolve fine (strict=False); on unexpected resolution
    errors fall back to the raw string comparison.
    """
    try:
        return Path(current).resolve() == Path(launch).resolve()
    except OSError:
        return str(current) == launch


class BackgroundAgentManager:
    """单例管理器，跟踪所有后台 subagent 的 asyncio.Task 句柄。

    纯生命周期管理器，不负责持久化：
    - 启动/取消 asyncio.Task
    - 向 EventBus/Trace 发送通知
    状态持久化由调用方（DelegateTool / delegate stop）负责。
    """

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
                    instance._event_bus = None
                    cls._instance = instance
        return cls._instance

    def set_event_bus(self, event_bus: Any) -> None:
        """Set the EventBus for publishing task completion notifications.

        会话级 CoaraBase 没有 event_bus（只有 RootCoara 有），完成通知必须走
        Root 启动时接进来的这条总线，否则发布被静默跳过、完成结果永不注入主会话。
        """
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
        """启动后台 subagent 任务，立即返回 task_id。

        Args:
            parent_coara: 父级 Coara，用于通知和 trace
            coro: 实际运行 subagent 的协程（通常由 DelegateToolInvocation 提供）
            task_id: 统一任务标识符（即 subagent_id，如 sa-coaras-xxx）
            subagent_type: subagent 类型（如 "aide"）
            description: 任务描述
            child_coara_id: 子代理的 coara_id，用于 CLI spinner 归属工具调用

        Returns:
            task_id: 与传入的 task_id 相同
        """
        self._tasks[task_id] = None  # placeholder before task starts
        launch_workspace_dir = str(parent_coara.workspace_dir)
        from src.coara.turn_source import current_turn_source

        origin_source = current_turn_source(parent_coara)
        # Capture coara_home at launch time so TaskStore terminal-state updates
        # write to the workspace that launched the delegate, even if the user
        # has since switched workspaces. Without this, task_store_for_workspace()
        # would resolve to the new workspace's TaskStore and the update would
        # silently fail (task_id not found), leaving the original workspace's
        # record stuck in RUNNING forever.
        _launch_wm = getattr(parent_coara, "workspace_manager", None)
        launch_coara_home = getattr(_launch_wm, "coara_home", None) if _launch_wm else None

        # Note: initial TaskStore write is handled by DelegateTool.execute()
        # to avoid duplicate writes and ensure consistent state.

        # 任务台账落盘：进程被杀后重启能发现「中断的子智能体任务」并提示用户
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
            """包装协程，负责完成后的通知和清理。"""
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

                # 提取结果：完整内容（注入主会话 + TaskStore；超限头尾截断并注明，无更长日志可回取）
                result_full = ""
                if hasattr(result, "content"):
                    result_full = str(result.content) if isinstance(result.content, str) else repr(result.content)
                elif isinstance(result, str):
                    result_full = result
                result_full = clip_result_for_persist(result_full)
                result_preview = result_full[:300]

                # 检测取消状态：coro 内部返回 ToolResult.cancelled() 或外部 CancelledError
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

                # Emit trace event for real-time UI notification
                if parent_coara is not None:
                    # 段归属：delegate 调用时父会话注入段来源（与子智能体 diff 同
                    # 锚点），随完成通知带给注入端，子智能体最终结果显示到该端。
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
                            # 发起端（web/matrix/cli/…）：CLI 完成横幅据此只显示本端
                            # 发起的任务——他端（如 web）发起的 aide 完成不在 CLI 镜像。
                            "origin_source": origin_source,
                            "subagent_origin": _sub_origin,
                            "subagent_origin_channel": _sub_channel,
                            # 发起空间归属（时刻在父实例上快照）：用户切走视图后
                            # 广播层据此把 complete 判为「其它空间」而非「其它会话」，
                            # 直接丢弃而不是标 detached 关行——活动树行随切视图已清。
                            "workspace_dir": launch_workspace_dir,
                        },
                    )

                # Publish completion notification to EventBus for root loop injection.
                # Only inject if the user is still in the same workspace that
                # launched the delegate — otherwise the notification would
                # pollute the new workspace's LLM context. The trace event and
                # TaskStore update above already record the completion for the
                # user to discover when they switch back.
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
                    # janitor/daily 是系统派发（结果落记录/ws.md，无父会话续接概念）；
                    # aide 与 coaras 同机制——子智能体收官必须回投主会话
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
                                    # 段归属：delegate 调用时父会话注入段来源 + matrix
                                    # 房间（子智能体最终结果显示到该端，与 diff 同锚点）。
                                    "subagent_origin": _sub_origin,
                                    "subagent_origin_channel": _sub_channel,
                                    # Launch-session stamps so Root routes the
                                    # completion back to the originating
                                    # workspace session, not the foreground.
                                    "coara_id": str(getattr(parent_coara.identity, "coara_id", "") or ""),
                                    "session_id": str(getattr(parent_coara, "session_id", "") or ""),
                                },
                            )
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to publish agent task completion: {exc}")

                # Persist terminal state so the record survives process restarts.
                # Use launch-time workspace_dir + coara_home so the update lands
                # in the original workspace's TaskStore even if the user has since
                # switched workspaces. Otherwise task_store_for_workspace() would
                # resolve to the new workspace, the task_id wouldn't be found, and
                # the original record would stay stuck in RUNNING forever.
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

                    # 任务台账终态：折叠后不再被重启扫描当作「中断」提示
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

        # Emit start trace event
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
        """Request a running background agent task to cancel.

        Returns True if the task was found and a cancellation was requested.
        A task_id present with a None value is a registered-but-not-yet-started
        placeholder: no asyncio.Task exists to cancel yet, so it is reported
        as not-started (False) rather than conflated with an unknown task_id.
        注意：TaskStore 状态更新由调用方负责。
        """
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

    def cancel_all(self) -> int:
        """Hard-cancel every registered background agent task (Ctrl+C / /stop).

        Placeholders (task not yet created) are marked so ``start`` cancels
        them as soon as the asyncio.Task appears. Returns how many running
        tasks were cancelled (placeholders counted too).
        """
        cancelled = 0
        for task_id, task in list(self._tasks.items()):
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
        """Await every registered background agent task, bounded by ``timeout``.

        与 cancel_all 配套：关停路径先 cancel_all 发取消，再 wait_all 等收尾，
        避免任务被进程退出强断（TaskStore 留 running、台账误报中断）。
        超时放弃等待，不让个别不响应取消的任务挂死关停。
        """
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

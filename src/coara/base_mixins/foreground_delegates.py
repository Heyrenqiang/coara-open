"""Foreground delegate tracking mixin.

宿主为 CoaraBase，属性在宿主 __init__ 初始化（mixin 不设 __init__，只做方法容器）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.core.types import Message, MessageRole


class ForegroundDelegateMixin:
    """前台子智能体跟踪：注册 / 完成回调 / 放行 / 取消"""

    def register_foreground_delegate(self, task_id: str, task: Any, description: str) -> None:
        """Register a foreground delegate task launched asynchronously.

        The turn loop's exit guard consults this registry to prevent the
        turn from exiting while foreground subagents are still running.
        """
        self._pending_foreground_delegates[task_id] = task
        self._foreground_delegate_descriptions[task_id] = description

    def on_foreground_delegate_done(self, task_id: str, description: str, task: Any) -> None:
        """Called when a foreground delegate asyncio.Task completes.

        Extracts the result and injects it as a continuation input so the
        next iteration sees it and can continue reasoning.

        Cancelled tasks are skipped: cancellation happens during
        ``_reset_transient_state`` / turn cleanup, so injecting a
        "子代理被取消" message would be stale noise.

        Released delegates（回合出口放行）: session busy → same continuation
        injection; session idle → park the result into session history (and
        persist) so the next turn sees it, without waking a new turn.
        """
        released = task_id in self._released_foreground_delegates
        self._pending_foreground_delegates.pop(task_id, None)
        self._released_foreground_delegates.pop(task_id, None)
        self._foreground_delegate_descriptions.pop(task_id, None)

        if task.cancelled():
            return

        from src.core.message_tags import system_info

        if task.exception() is not None:
            exc = task.exception()
            # str(exc) 可为空串（如裸 CancelledError/Error()）：回退异常类型名，保证失败信息非空可辨
            result_text = f"子代理失败: {str(exc) or type(exc).__name__}"
        else:
            tool_result = task.result()
            # _run_subagent swallows CancelledError and returns ToolResult.cancelled
            # — treat that like task.cancelled() so interrupt cleanup stays quiet.
            if getattr(tool_result, "is_cancelled", False):
                return
            content = getattr(tool_result, "content", None)
            result_text = str(content) if content is not None else str(tool_result)

        message = system_info(f"[前台子智能体已完成] [{task_id}]\n任务：{description}\n结果：{result_text}")
        # 段归属：delegate 调用时父会话注入段来源 + matrix 房间，随结果注入传给
        # turn loop（与子智能体 diff 同锚点；收官直推不靠 ContextVar）。
        sub_origin = ""
        sub_channel = ""
        try:
            result = task.result()
            meta = getattr(result, "metadata", None) or {}
            sub_origin = str(meta.get("subagent_origin") or "")
            sub_channel = str(meta.get("subagent_origin_channel") or "")
        except Exception:  # noqa: BLE001
            sub_origin = ""
            sub_channel = ""
        if released and not self.has_active_turn():
            # 放行后的迟到结果：会话空闲 → 进驻历史（下一轮可见），不唤醒新回合
            self.message_history.append(Message(role=MessageRole.USER, content=message))
            try:
                loop = asyncio.get_running_loop()
                # 落盘任务必须持引用：fire-and-forget 任务随时可能被 GC
                # 回收协程，落盘静默丢失
                persist_task = loop.create_task(asyncio.to_thread(self.persist_session_to_disk))
                self._session_persist_tasks.add(persist_task)
                persist_task.add_done_callback(self._session_persist_tasks.discard)
            except RuntimeError:
                pass
            return
        self.submit_continuation_input(
            message,
            agent_origin=sub_origin,
            agent_origin_channel=sub_channel,
        )

    def release_pending_foreground_delegates(self) -> list[str]:
        """放行全部在跑的前台子智能体：回合可正常结束，完成结果按迟到语义路由。

        Returns the released task_ids (still running only).
        """
        released: list[str] = []
        for task_id, task in list(self._pending_foreground_delegates.items()):
            if task.done():
                continue
            self._released_foreground_delegates[task_id] = task
            self._pending_foreground_delegates.pop(task_id, None)
            released.append(task_id)
        return released

    def has_pending_foreground_delegates(self) -> bool:
        """True if any foreground delegate task is still running."""
        # Clean up finished tasks first
        done_ids = [tid for tid, task in self._pending_foreground_delegates.items() if task.done()]
        for tid in done_ids:
            self._pending_foreground_delegates.pop(tid, None)
            self._foreground_delegate_descriptions.pop(tid, None)
        return bool(self._pending_foreground_delegates)

    def pending_foreground_delegate_descriptions(self) -> str:
        """Return a formatted list of pending foreground delegate descriptions."""
        lines = []
        for task_id, desc in self._foreground_delegate_descriptions.items():
            lines.append(f"- [{task_id}] {desc}")
        return "\n".join(lines)

    def cancel_all_pending_foreground_delegates(self, *, include_released: bool = True) -> None:
        """Cancel all running foreground delegate tasks (e.g. on turn interrupt).

        include_released=True（中断路径）：连放行的一起停，Ctrl+C 语义不变。
        include_released=False（回合正常退出的 finally）：放行的继续跑，
        迟到结果经 done 回调驻留历史。
        """
        tasks = list(self._pending_foreground_delegates.items())
        if include_released:
            tasks += list(self._released_foreground_delegates.items())
        for task_id, task in tasks:
            if not task.done():
                task.cancel()
                # Emit failed so CLI spinner drops the live node. Tasks cancelled
                # before ``_run_subagent`` runs never emit subagent_failed themselves
                # (seen when /new raced fire-and-forget foreground_async janitor).
                self._emit_trace(
                    "subagent_failed",
                    f"Subagent cancelled: {task_id}",
                    payload={
                        "subagent_id": task_id,
                        "description": self._foreground_delegate_descriptions.get(task_id, ""),
                        "error": "cancelled",
                        "workspace_dir": str(getattr(self, "workspace_dir", "") or ""),
                    },
                )
        self._pending_foreground_delegates.clear()
        if include_released:
            self._released_foreground_delegates.clear()
        self._foreground_delegate_descriptions.clear()

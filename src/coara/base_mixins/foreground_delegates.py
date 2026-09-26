"""Foreground delegate tracking mixin. Attributes live on CoaraBase.__init__."""

from __future__ import annotations

import asyncio
from typing import Any

from src.core.types import Message, MessageRole


class ForegroundDelegateMixin:
    """前台子智能体：注册 / 完成回调 / 放行 / 取消。"""

    def register_foreground_delegate(self, task_id: str, task: Any, description: str) -> None:
        """Register a foreground delegate; turn exit waits on this registry."""
        self._pending_foreground_delegates[task_id] = task
        self._foreground_delegate_descriptions[task_id] = description

    def on_foreground_delegate_done(self, task_id: str, description: str, task: Any) -> None:
        """Foreground Task done → continuation; cancelled silent; released+idle → awaken."""
        released = task_id in self._released_foreground_delegates
        self._pending_foreground_delegates.pop(task_id, None)
        self._released_foreground_delegates.pop(task_id, None)
        self._foreground_delegate_descriptions.pop(task_id, None)

        if task.cancelled():
            return

        from src.core.message_tags import system_info

        if task.exception() is not None:
            exc = task.exception()
            # str(exc) may be empty (bare CancelledError/Error()); fall back to type name
            result_text = f"子代理失败: {str(exc) or type(exc).__name__}"
        else:
            tool_result = task.result()
            # _run_subagent swallows CancelledError → ToolResult.cancelled; treat as cancelled
            if getattr(tool_result, "is_cancelled", False):
                return
            content = getattr(tool_result, "content", None)
            result_text = str(content) if content is not None else str(tool_result)

        message = system_info(f"[前台子智能体已完成] [{task_id}]\n任务：{description}\n结果：{result_text}")
        # Segment origin from delegate metadata (same anchor as subagent diff; no ContextVar)
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
            # Released + idle → awaken LLM; no root → park in history only
            root = getattr(self, "_root_ref", None)
            awaken = getattr(root, "_awaken_background_turn", None) if root is not None else None
            if awaken is not None:
                try:
                    loop = asyncio.get_running_loop()
                    origin = sub_origin or str(getattr(self, "_last_user_input_source", "") or "")
                    wake_task = loop.create_task(awaken(self, task_id, message, origin_source=origin))
                    bg = getattr(root, "_bg_tasks", None)
                    if isinstance(bg, set):
                        bg.add(wake_task)
                        wake_task.add_done_callback(bg.discard)
                    return
                except RuntimeError:
                    pass
            self.message_history.append(Message(role=MessageRole.USER, content=message))
            try:
                loop = asyncio.get_running_loop()
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
        """Release running foreground delegates; late results route as late arrivals."""
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
        done_ids = [tid for tid, task in self._pending_foreground_delegates.items() if task.done()]
        for tid in done_ids:
            self._pending_foreground_delegates.pop(tid, None)
            self._foreground_delegate_descriptions.pop(tid, None)
        return bool(self._pending_foreground_delegates)

    def pending_foreground_delegate_descriptions(self) -> str:
        """Formatted list of pending foreground delegate descriptions."""
        lines = []
        for task_id, desc in self._foreground_delegate_descriptions.items():
            lines.append(f"- [{task_id}] {desc}")
        return "\n".join(lines)

    def cancel_all_pending_foreground_delegates(self, *, include_released: bool = True) -> None:
        """Cancel running foreground delegates. include_released=True on interrupt; False keeps released running."""
        tasks = list(self._pending_foreground_delegates.items())
        if include_released:
            tasks += list(self._released_foreground_delegates.items())
        for task_id, task in tasks:
            if not task.done():
                task.cancel()
                # Emit failed so CLI spinner drops the live node
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

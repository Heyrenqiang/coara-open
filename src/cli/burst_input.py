"""Detect and annotate rapid consecutive user messages in the CLI queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src.cli.input_queue_display import is_queueable_user_text
from src.core.message_tags import system_reminder


@dataclass(slots=True)
class ChatTurnInput:
    """One user message ready for a Root turn."""

    text: str
    burst_index: int = 1
    burst_total: int = 1
    image_blocks: list[dict[str, Any]] = field(default_factory=list)


def _drain_queued_user_text(queue: asyncio.Queue[Any]) -> list[str]:
    """Drain consecutive plain user messages already waiting in the queue."""
    extra: list[str] = []
    while True:
        try:
            nxt = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if is_queueable_user_text(nxt):
            extra.append(nxt.strip())
        else:
            queue.put_nowait(nxt)
            break
    return extra


def _finalize_chat_turn_input(result: ChatTurnInput) -> ChatTurnInput:
    return result


@dataclass(slots=True)
class BurstInputState:
    """In-flight burst batch owned by one chat input loop.

    Previously a module-global list shared by every consumer; each chat
    runner now owns its own state so queued bursts can never leak across
    sessions or instances.
    """

    _pending: list[tuple[int, int, list[str]]] = field(default_factory=list)

    async def next_chat_turn_input(self, queue: asyncio.Queue[Any]) -> ChatTurnInput | Any:
        """Return the next chat input, batching queued burst messages when present.

        Sentinel objects (_PromptExit, _PromptCtrlC) are returned unchanged.
        """
        if self._pending:
            index, total, texts = self._pending[0]
            text = texts[0]
            rest = texts[1:]
            if rest:
                self._pending[0] = (index + 1, total, rest)
            else:
                self._pending.pop(0)
            return _finalize_chat_turn_input(ChatTurnInput(text=text, burst_index=index, burst_total=total))

        first = await queue.get()
        if not is_queueable_user_text(first):
            return first

        text = first.strip()
        extra = _drain_queued_user_text(queue)
        total = 1 + len(extra)
        if total == 1:
            return _finalize_chat_turn_input(ChatTurnInput(text=text))

        if extra:
            self._pending.append((2, total, extra))
        return _finalize_chat_turn_input(ChatTurnInput(text=text, burst_index=1, burst_total=total))

    def reset(self) -> None:
        """Clear pending burst batch."""
        self._pending.clear()


# Default state for callers that do not own a BurstInputState (backwards
# compatibility for the module-level API and test helpers).
_default_state = BurstInputState()


async def next_chat_turn_input(queue: asyncio.Queue[Any]) -> ChatTurnInput | Any:
    """Return the next chat input, batching queued burst messages when present.

    Sentinel objects (_PromptExit, _PromptCtrlC) are returned unchanged.
    """
    return await _default_state.next_chat_turn_input(queue)


def annotate_burst_message(content: str, *, burst_index: int, burst_total: int) -> str:
    """Prepend a system reminder when the user sent multiple messages before the prior reply."""
    if burst_total <= 1 or burst_index <= 1:
        return content
    note = system_reminder(
        f"用户在上一则回复完成前连发了 {burst_total} 条消息。"
        f"当前处理第 {burst_index}/{burst_total} 条。"
        "请结合此前对话理解本条；若后文是在补充、修正或催促，以最新信息为准。"
    )
    return f"{note}\n\n{content}"

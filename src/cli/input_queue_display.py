"""CLI feedback for user messages queued while a turn is running."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.types import ContinuationInput


def _item_text(item: Any) -> str:
    """Continuation queue item → user text (handles ``ContinuationInput``)."""
    if isinstance(item, ContinuationInput):
        return item.text
    return str(item or "")


# Soft cap so a pasted novel does not flood the spinner; still per-message lines.
_HINT_MAX_CHARS_PER_MESSAGE = 2000


@dataclass(frozen=True, slots=True)
class PoppedFollowup:
    """Esc 撤下的一条用户跟话。纯图时 text 为空，靠 client_msg_id 让内核删同一条。"""

    text: str
    client_msg_id: str = ""


def _continuation_queue(root: Any) -> list[ContinuationInput] | None:
    """Return the active session's continuation queue (foreground coara)."""
    if root is None:
        return None
    target = getattr(root, "foreground_coara", root)
    inputs = getattr(target, "_continuation_inputs", None)
    if not isinstance(inputs, (list, tuple)):
        return None
    return list(inputs)


def _message_display_line(text: str) -> str:
    """One logical line for one queued message (full text, internal newlines flattened)."""
    cleaned = str(text or "")
    one_line = " ".join(cleaned.split())
    if len(one_line) > _HINT_MAX_CHARS_PER_MESSAGE:
        return one_line[: _HINT_MAX_CHARS_PER_MESSAGE - 1] + "…"
    return one_line


def is_queueable_user_text(item: Any) -> bool:
    return isinstance(item, str) and bool(item.strip()) and not item.strip().startswith("/")


def _user_followups(inputs: list[Any]) -> list[Any]:
    """User mid-turn follow-ups only — skip system/event injections (bg complete, etc.)."""
    from src.core.message_tags import continuation_followup_display_line, is_preformatted_injection

    followups: list[Any] = []
    for item in inputs:
        text = _item_text(item)
        if is_preformatted_injection(text):
            continue
        images = item.image_blocks if isinstance(item, ContinuationInput) else None
        if continuation_followup_display_line(text, image_blocks=images):
            followups.append(item)
    return followups


def _followup_hint_line(item: Any) -> str | None:
    from src.core.message_tags import continuation_followup_display_line

    text = _item_text(item)
    images = item.image_blocks if isinstance(item, ContinuationInput) else None
    display = continuation_followup_display_line(text, image_blocks=images)
    if not display:
        return None
    # 纯图占位符保持短标签；文本跟话压单行（图片标记留在末尾）。
    if display.startswith("[图片"):
        return display
    return _message_display_line(display)


def pending_input_hint_lines(root: Any) -> list[str]:
    """Queued follow-ups as ``→ text`` lines (no header). Empty when nothing to show."""
    if root is None:
        return []
    fg = getattr(root, "foreground_coara", root)
    if not getattr(fg, "has_active_turn", lambda: False)():
        return []
    inputs = _continuation_queue(root)
    if not inputs:
        return []
    followups = _user_followups(inputs)
    if not followups:
        return []
    lines: list[str] = []
    for item in followups:
        body = _followup_hint_line(item)
        if body:
            lines.append(f"→ {body}")
    return lines


def has_popable_queued_followup(root: Any) -> bool:
    """Esc 取消注入的前置条件：回合运行中且队列里有用户跟话（系统注入不算）。"""
    if root is None:
        return False
    fg = getattr(root, "foreground_coara", root)
    if not getattr(fg, "has_active_turn", lambda: False)():
        return False
    inputs = getattr(fg, "_continuation_inputs", None)
    if not isinstance(inputs, list):
        return False
    return bool(_user_followups(inputs))


def pop_latest_queued_followup(root: Any) -> PoppedFollowup | None:
    """移除队尾最新一条排队待注入的用户跟话（系统注入不动）"""
    if root is None:
        return None
    fg = getattr(root, "foreground_coara", root)
    inputs = getattr(fg, "_continuation_inputs", None)
    if not isinstance(inputs, list):
        return None
    from src.core.message_tags import is_preformatted_injection

    for index in range(len(inputs) - 1, -1, -1):
        item = inputs[index]
        text = _item_text(item)
        if is_preformatted_injection(text):
            continue
        if _followup_hint_line(item):
            del inputs[index]
            return PoppedFollowup(
                text=text.strip(),
                client_msg_id=str(getattr(item, "client_msg_id", "") or ""),
            )
    return None

"""Turn exit control for the internal LLM-tool loop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Assistant reply at or above this size is treated as turn delivery even if todos remain open.
TODO_SUBSTANTIAL_DELIVERY_CHARS = 400

# Short text-only replies with no todo progress: allow one continue, stop on the second.
# (Identical consecutive reply text also stops immediately — see decide_after_llm.)
MAX_TODO_STALL_CONTINUES = 2


class TurnAction(StrEnum):
    """What the internal loop should do after one LLM turn."""

    CONTINUE = "continue"
    RETURN_FINAL = "return_final"


@dataclass(slots=True)
class TodoLoopState:
    """Minimal todo runtime state relevant to loop termination."""

    total: int = 0
    completed: int = 0
    has_active: bool = False
    has_pending: bool = False
    # Fingerprint of open work; unchanged across short text-only continues ⇒ stall.
    progress_key: str = ""


@dataclass(slots=True)
class TurnState:
    """Inputs used by the turn controller to decide loop flow."""

    has_tool_calls: bool
    todo: TodoLoopState
    assistant_text_chars: int = 0
    assistant_text: str = ""


@dataclass(slots=True)
class TurnDecision:
    """Decision returned by the turn controller."""

    action: TurnAction
    reason: str


def _normalize_continue_text(text: str) -> str:
    """Collapse whitespace so trivial formatting diffs still count as the same reply."""
    return " ".join((text or "").split())


class TurnController:
    """Controls whether the internal loop should continue or exit.

    Incomplete todos may keep the loop alive after a short text-only reply, but:

    - **Same reply text twice in a row** → stop (``todo_incomplete_duplicate_reply``)
    - **Two short continues with no todo progress** → stop (``todo_incomplete_stalled``)

    (``todo`` action="park" bypasses this controller entirely: the orchestrator
    ends the turn immediately after the tool batch that carried the park call.)

    Tool-call iterations keep the loop alive, but only an actual todo progress
    (``progress_key`` change) clears the no-progress streak — polling-style
    calls do not, so text-only spin between polls still trips the guard.

    Call ``reset_turn()`` at each user-turn start.
    """

    def __init__(self) -> None:
        self._todo_stall_streak: int = 0
        self._last_todo_progress_key: str = ""
        self._last_continue_text: str = ""

    @property
    def todo_stall_streak(self) -> int:
        return self._todo_stall_streak

    def reset_turn(self) -> None:
        """Clear per-turn stall tracking (call at the start of ``process_message``)."""
        self._todo_stall_streak = 0
        self._last_todo_progress_key = ""
        self._last_continue_text = ""

    def decide_after_llm(self, state: TurnState) -> TurnDecision:
        if state.has_tool_calls:
            # Only real todo progress (fingerprint change) earns a fresh
            # short-text streak; polling/inspection calls must not reset it.
            if state.todo.progress_key != self._last_todo_progress_key:
                self._todo_stall_streak = 0
                self._last_todo_progress_key = state.todo.progress_key
            self._last_continue_text = ""
            return TurnDecision(action=TurnAction.CONTINUE, reason="tool_calls_present")

        if state.todo.total > 0 and (state.todo.has_active or state.todo.has_pending):
            if state.assistant_text_chars >= TODO_SUBSTANTIAL_DELIVERY_CHARS:
                self._todo_stall_streak = 0
                self._last_todo_progress_key = state.todo.progress_key
                self._last_continue_text = ""
                return TurnDecision(
                    action=TurnAction.RETURN_FINAL,
                    reason="todo_incomplete_substantial_reply",
                )

            text_key = _normalize_continue_text(state.assistant_text)
            # Principle: identical consecutive continue text → stop immediately.
            if text_key and text_key == self._last_continue_text:
                return TurnDecision(
                    action=TurnAction.RETURN_FINAL,
                    reason="todo_incomplete_duplicate_reply",
                )

            key = state.todo.progress_key
            if key != self._last_todo_progress_key:
                self._last_todo_progress_key = key
                self._todo_stall_streak = 1
            else:
                self._todo_stall_streak += 1
            self._last_continue_text = text_key
            if self._todo_stall_streak >= MAX_TODO_STALL_CONTINUES:
                return TurnDecision(
                    action=TurnAction.RETURN_FINAL,
                    reason="todo_incomplete_stalled",
                )
            return TurnDecision(action=TurnAction.CONTINUE, reason="todo_incomplete")

        self._todo_stall_streak = 0
        self._last_todo_progress_key = ""
        self._last_continue_text = ""
        return TurnDecision(action=TurnAction.RETURN_FINAL, reason="no_tool_calls_and_no_pending_state")

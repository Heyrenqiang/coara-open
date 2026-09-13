"""P0-9: rollback after mid-turn compress must not use a stale absolute index."""

from __future__ import annotations

from src.core.types import Message, MessageRole
from tests.helpers import make_test_coara


def test_rollback_after_history_rewrite_keeps_compressed_prefix(tmp_path) -> None:
    """Compress rewrites the list; rollback must keep the new prefix, not del[old_start:]."""
    coara = make_test_coara(tmp_path)
    coara.message_history = [
        Message(role=MessageRole.USER, content=f"old-{i}") for i in range(80)
    ]
    turn_history_start = 40  # absolute index into the pre-compress list

    # Mid-turn compress: short rewritten history (summary + recent)
    coara.message_history = [
        Message(role=MessageRole.USER, content="<state_snapshot>kept</state_snapshot>"),
        Message(role=MessageRole.USER, content="recent-user"),
        Message(role=MessageRole.ASSISTANT, content="recent-assistant"),
    ]
    coara.note_history_rewrite()
    assert coara._rollback_floor == 3
    assert coara._history_epoch == 1

    # Partial turn after compress
    coara.message_history.append(Message(role=MessageRole.ASSISTANT, content="ghost-partial"))
    coara.message_history.append(Message(role=MessageRole.USER, content="tool-noise"))

    coara._rollback_partial_turn_history(turn_history_start)

    texts = [m.content for m in coara.message_history]
    assert texts == [
        "<state_snapshot>kept</state_snapshot>",
        "recent-user",
        "recent-assistant",
    ]
    assert "ghost-partial" not in texts
    # Stale index 40 must not wipe the compressed prefix
    assert "<state_snapshot>kept</state_snapshot>" in texts


def test_rollback_without_rewrite_still_uses_absolute_start(tmp_path) -> None:
    coara = make_test_coara(tmp_path)
    coara.message_history = [
        Message(role=MessageRole.USER, content="keep-0"),
        Message(role=MessageRole.ASSISTANT, content="keep-1"),
        Message(role=MessageRole.USER, content="turn-user"),
        Message(role=MessageRole.ASSISTANT, content="turn-partial"),
    ]
    coara._rollback_partial_turn_history(2)
    assert [m.content for m in coara.message_history] == ["keep-0", "keep-1"]


def test_clear_rollback_floor_restores_absolute_rollback(tmp_path) -> None:
    coara = make_test_coara(tmp_path)
    coara.message_history = [Message(role=MessageRole.USER, content="a")]
    coara.note_history_rewrite()
    coara.clear_rollback_floor()
    coara.message_history.extend(
        [
            Message(role=MessageRole.USER, content="b"),
            Message(role=MessageRole.ASSISTANT, content="c"),
        ]
    )
    coara._rollback_partial_turn_history(1)
    assert [m.content for m in coara.message_history] == ["a"]

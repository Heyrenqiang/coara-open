"""Regression tests for LoopDetector (src/agent/loop.py).

Covers the alternating-pattern false positive where any call following a
different tool was treated as a length-2 alternation, making threshold-2
tools unusable on their first invocation of a turn.
"""

from __future__ import annotations

from src.agent import loop as loop_mod
from src.agent.loop import LoopDetector
from src.core.types import ToolCall

_LOW = "media"


def _call(name: str, args: dict | None = None, *, call_id: str = "c") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args or {})


def _record(detector: LoopDetector, name: str, args: dict | None = None) -> None:
    detector.record(_call(name, args), successful=True)


def test_first_low_threshold_call_after_other_tools_is_not_blocked() -> None:
    detector = LoopDetector()
    _record(detector, "todo", {"action": "add"})
    _record(detector, "shell", {"command": "git status"})

    alert = detector.should_block(_call(_LOW, {"action": "status"}), None)
    assert alert is None


def test_low_threshold_call_with_different_arguments_is_not_blocked() -> None:
    detector = LoopDetector()
    _record(detector, "shell", {"command": "git status"})
    _record(detector, _LOW, {"action": "status"})

    alert = detector.should_block(_call(_LOW, {"action": "cancel", "task_id": "vq-1"}), None)
    assert alert is None


def test_repeated_identical_low_threshold_call_is_blocked() -> None:
    detector = LoopDetector()
    _record(detector, "shell", {"command": "git status"})
    _record(detector, _LOW, {"action": "status"})

    # media threshold is 2: the second identical call in a row is blocked.
    alert = detector.should_block(_call(_LOW, {"action": "status"}), None)
    assert alert is not None
    assert alert.tool_name == _LOW


def test_single_ab_alternation_is_not_blocked() -> None:
    detector = LoopDetector()
    _record(detector, "read", {"path": "/a"})
    _record(detector, "read", {"path": "/b"})

    # A,B then A: one and a half cycle, just a tool switch, must not block.
    alert = detector.should_block(_call("read", {"path": "/a"}), None)
    assert alert is None


def test_true_ping_pong_loop_is_blocked() -> None:
    detector = LoopDetector()
    _record(detector, "read", {"path": "/a"})
    _record(detector, "read", {"path": "/b"})
    _record(detector, "read", {"path": "/a"})

    # History ends A,B,A; the candidate B completes two full A/B cycles.
    alert = detector.should_block(_call("read", {"path": "/b"}), None)
    assert alert is not None
    assert alert.tool_name == "read"


def test_ping_pong_of_low_threshold_tool_is_blocked() -> None:
    detector = LoopDetector()
    _record(detector, _LOW, {"action": "status"})
    _record(detector, "shell", {"command": "git status"})
    _record(detector, _LOW, {"action": "status"})
    _record(detector, "shell", {"command": "git status"})

    # media,shell ×2 then media again: a genuine ping-pong pattern.
    alert = detector.should_block(_call(_LOW, {"action": "status"}), None)
    assert alert is not None


def test_interleaved_repeat_cycle_is_blocked_by_window_count() -> None:
    """5×A, 1×B, 4×A evades the trailing-streak check; the sliding-window
    cumulative count (2× streak threshold) must still catch it."""
    detector = LoopDetector()
    poll = {"command": "git log --oneline -1; git status --porcelain"}
    for _ in range(4):
        _record(detector, "shell", poll)
    _record(detector, _LOW, {"action": "status"})
    for _ in range(4):
        _record(detector, "shell", poll)
    _record(detector, _LOW, {"action": "status"})
    _record(detector, "shell", poll)

    # Cumulative = 9 within the window; shell limit is 5*2=10 → still allowed.
    alert = detector.should_block(_call("shell", poll), None)
    assert alert is None
    _record(detector, "shell", poll)

    # Cumulative hits 10 while the trailing streak is only 1 → window count blocks.
    alert = detector.should_block(_call("shell", poll), None)
    assert alert is not None
    assert alert.tool_name == "shell"


def test_scattered_legit_repeats_below_window_limit_are_allowed() -> None:
    detector = LoopDetector()
    test_cmd = {"command": "python -m pytest tests/ -q"}
    for i in range(3):
        _record(detector, "shell", test_cmd)
        _record(detector, "edit", {"path": f"/f{i}.py"})
        _record(detector, "shell", test_cmd)
        _record(detector, "read", {"path": f"/f{i}.py"})

    # 6 scattered identical runs; trailing streak is 1, window count 6 < 10.
    alert = detector.should_block(_call("shell", test_cmd), None)
    assert alert is None


def test_shell_loop_feedback_teaches_sleep_polling() -> None:
    detector = LoopDetector()
    poll = {"command": "git status --porcelain"}
    for _ in range(4):
        _record(detector, "shell", poll)

    alert = detector.should_block(_call("shell", poll), None)
    assert alert is not None
    assert "Start-Sleep" in alert.feedback
    assert "background" in alert.feedback


def test_delegate_wait_is_never_blocked() -> None:
    detector = LoopDetector()
    for _ in range(20):
        assert detector.should_block(_call("delegate", {"action": "wait"}), None) is None
        _record(detector, "delegate", {"action": "wait"})


def test_delegate_wait_does_not_pollute_window_counts() -> None:
    detector = LoopDetector()
    for _ in range(20):
        _record(detector, "delegate", {"action": "wait"})
    # Other tools must be unaffected by a long streak of waits.
    assert detector.should_block(_call("shell", {"command": "git status"}), None) is None


def test_delegate_non_wait_actions_still_counted() -> None:
    detector = LoopDetector()
    for _ in range(10):
        _record(detector, "delegate", {"action": "list"})
    alert = detector.should_block(_call("delegate", {"action": "list"}), None)
    assert alert is not None
    assert alert.tool_name == "delegate"


def test_retired_task_tool_has_no_special_threshold() -> None:
    assert "task" not in loop_mod.TOOL_TYPE_THRESHOLDS
    assert loop_mod.TOOL_TYPE_THRESHOLDS.get(_LOW) == 2

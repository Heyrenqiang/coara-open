"""Attach CLI 跟话排队镜像：子智能体结果注入不得清掉用户已排队的跟话。"""

from __future__ import annotations

from src.cli.root_shim import ForegroundCoaraShim
from src.core.events import TraceEvent
from src.core.types import ContinuationInput


def test_subagent_only_inject_preserves_local_followup_queue() -> None:
    fg = ForegroundCoaraShim.__new__(ForegroundCoaraShim)
    fg._active_turn_source = "cli-attached"
    fg._continuation_inputs = [
        ContinuationInput(text="继续", source="cli-attached", client_msg_id="m1"),
    ]
    fg._injected_texts = set()
    fg._injected_ids = set()

    event = TraceEvent(
        coara_id="root",
        coara_name="考拉",
        event_type="continuation_input_injected",
        message="Injected",
        payload={
            "user_texts": [],
            "user_sources": [],
            "subagent_texts": ["[前台子智能体已完成] 调研结果"],
            "subagent_sources": ["cli-attached"],
        },
    )
    fg._on_continuation_injected(event)

    assert len(fg._continuation_inputs) == 1
    assert fg._continuation_inputs[0].text == "继续"


def test_user_inject_removes_matching_followup() -> None:
    fg = ForegroundCoaraShim.__new__(ForegroundCoaraShim)
    fg._active_turn_source = "cli-attached"
    fg._continuation_inputs = [
        ContinuationInput(text="继续", source="cli-attached", client_msg_id="m1"),
        ContinuationInput(text="另一条", source="cli-attached", client_msg_id="m2"),
    ]
    fg._injected_texts = set()
    fg._injected_ids = set()

    event = TraceEvent(
        coara_id="root",
        coara_name="考拉",
        event_type="continuation_input_injected",
        message="Injected",
        payload={
            "user_texts": ["继续"],
            "user_sources": ["cli-attached"],
            "client_msg_ids": ["m1"],
        },
    )
    fg._on_continuation_injected(event)

    assert [item.text for item in fg._continuation_inputs] == ["另一条"]
    assert fg._active_turn_source == "cli-attached"


def test_clear_root_status_keeps_running_delegate() -> None:
    from src.cli.activity_live import ActivityLiveTracker
    from src.core.events import TraceEvent

    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root",
            coara_name="考拉",
            event_type="tool_start",
            message="",
            payload={
                "tool_name": "delegate",
                "tool_call_id": "d1",
                "arguments": {"subagent_type": "aide", "description": "调研"},
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root",
            coara_name="考拉",
            event_type="subagent_start",
            message="",
            payload={
                "subagent_id": "sa-1",
                "subagent_type": "aide",
                "description": "调研",
                "parent_tool_call_id": "d1",
                "child_coara_id": "sa-coara",
            },
        )
    )
    assert tracker.has_active_blocks()
    tracker.clear_root_status()
    assert tracker.has_active_blocks()
    rows = tracker.get_status_rows()
    assert any("aide" in line or "调研" in line for line, _ in rows)


def test_clear_root_status_keeps_running_flow() -> None:
    from src.cli.activity_live import ActivityLiveTracker
    from src.core.events import TraceEvent

    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root",
            coara_name="考拉",
            event_type="flow_started",
            message="",
            payload={"flow": "调研流"},
        )
    )
    assert tracker.has_active_blocks()
    tracker.clear_root_status()
    assert tracker.has_active_blocks()
    assert any("调研流" in line for line, _ in tracker.get_status_rows())


def test_mark_turn_end_keeps_injected_sentinels() -> None:
    fg = ForegroundCoaraShim.__new__(ForegroundCoaraShim)
    fg._active_turn = True
    fg._active_turn_source = "cli-attached"
    fg._continuation_inputs = [ContinuationInput(text="leftover", source="cli-attached")]
    fg._injected_texts = {"已注入"}
    fg._injected_ids = {"id-1"}
    fg._mark_turn_end(TraceEvent(coara_id="r", coara_name="", event_type="completed", message=""))
    assert fg._continuation_inputs[0].text == "leftover"
    assert fg._injected_texts == {"已注入"}
    assert fg._injected_ids == {"id-1"}
    # 迟到 received 仍被哨兵挡住
    fg._on_continuation_received(
        TraceEvent(
            coara_id="r",
            coara_name="",
            event_type="continuation_input_received",
            message="",
            payload={"text": "已注入", "client_msg_id": "id-1", "source": "cli-attached"},
        )
    )
    assert len(fg._continuation_inputs) == 1

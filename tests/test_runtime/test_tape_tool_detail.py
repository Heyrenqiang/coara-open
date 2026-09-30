"""录像带 tool 详情字段：参数优先级、结果预览上限、spill 引用。"""

from __future__ import annotations

from src.runtime.tape_tool_detail import apply_tape_tool_detail, pick_tape_arguments


def test_pick_tape_args_prefers_tape_over_usage() -> None:
    assert pick_tape_arguments(
        {
            "usage_args": {"path": "/a"},
            "tape_args": {"path": "/a", "contents": "x"},
        }
    ) == {"path": "/a", "contents": "x"}


def test_pick_tape_args_falls_back_to_arguments() -> None:
    assert pick_tape_arguments({"arguments": {"path": "/b"}}) == {"path": "/b"}


def test_apply_caps_large_output_and_keeps_ref() -> None:
    target: dict = {}
    big = "x" * 20_000
    apply_tape_tool_detail(
        target,
        {
            "tape_args": {"path": "/a"},
            "tool_output": big,
            "tool_output_ref": "spill/1",
            "tool_output_truncated": False,
        },
    )
    assert target["arguments"] == {"path": "/a"}
    assert target["tool_output_ref"] == "spill/1"
    assert target["tool_output_truncated"] is True
    assert len(target["tool_output"]) < len(big)
    assert target["tool_output"].endswith("…[截断]")


def test_apply_preserves_upstream_truncated_flag() -> None:
    target: dict = {}
    apply_tape_tool_detail(
        target,
        {
            "tool_output": "short",
            "tool_output_truncated": True,
            "tool_output_ref": "spill/2",
        },
    )
    assert target["tool_output"] == "short"
    assert target["tool_output_truncated"] is True
    assert target["tool_output_ref"] == "spill/2"

"""Tests for layered spill policy (vendor-aligned thresholds)."""

from __future__ import annotations

import pytest

from src.runtime.spill_policy import SpillKeep, build_spill_preview, resolve_spill_policy
from tests.test_runtime.conftest import make_tool_output_settings


@pytest.mark.parametrize(
    "tool,category,threshold,keep",
    [
        ("read", None, None, SpillKeep.BOTH),
        ("shell", None, 30_000, SpillKeep.TAIL),
        ("grep", None, 20_000, SpillKeep.BOTH),
        ("delegate", None, 32_000, SpillKeep.TAIL),
    ],
)
def test_resolve_spill_policy(tool, category, threshold, keep) -> None:
    got_threshold, got_keep = resolve_spill_policy(
        tool,
        make_tool_output_settings(),
        tool_category=category,
    )
    assert got_threshold == threshold
    assert got_keep == keep


def test_build_spill_preview_modes() -> None:
    both_body = ("HEAD\n" * 50) + ("MIDDLE\n" * 200) + ("TAIL\n" * 50)
    both = build_spill_preview(both_body, head_chars=20, tail_chars=20, keep=SpillKeep.BOTH)
    assert "PREVIEW TRUNCATED" in both and both.startswith("HEAD") and both.endswith("TAIL\n")

    tail_body = "START\n" + ("x" * 100) + "\nFAILED at end"
    tail = build_spill_preview(tail_body, head_chars=10, tail_chars=30, keep=SpillKeep.TAIL)
    assert "FAILED" in tail and "START" not in tail

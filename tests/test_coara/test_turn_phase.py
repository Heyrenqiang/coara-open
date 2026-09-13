"""Tests for the per-turn phase tracker (src/coara/turn_phase.py)."""

from __future__ import annotations

from src.coara.turn_phase import TurnPhase


def test_empty_phase_describes_nothing() -> None:
    assert TurnPhase().describe() == ""


def test_awaiting_llm_describe() -> None:
    phase = TurnPhase()
    phase.set("awaiting_llm", "kimi/k3")
    desc = phase.describe(now=phase.since + 161)
    assert desc.startswith("等待模型响应 kimi/k3（2m 41s")


def test_first_chunk_switches_to_receiving_and_counts() -> None:
    phase = TurnPhase()
    phase.set("awaiting_llm", "kimi/k3")
    phase.note_chunk()
    assert phase.phase == "receiving_llm"
    assert phase.detail == "kimi/k3"  # provider/model 保留
    assert phase.chunk_count == 1
    phase.note_chunk()
    phase.note_chunk()
    desc = phase.describe(now=phase.since + 35)
    assert desc == "接收模型响应 kimi/k3（35s，已收 3 分片）"


def test_tool_running_then_processing() -> None:
    phase = TurnPhase()
    phase.set("tool_running", "shell, read")
    assert phase.describe(now=phase.since + 35) == "执行工具 shell, read（35s）"
    phase.set("processing")
    assert phase.describe(now=phase.since + 5) == "本地处理中（5s）"
    # 切相位后分片计数归零
    assert phase.chunk_count == 0


def test_same_phase_set_is_noop() -> None:
    phase = TurnPhase()
    phase.set("awaiting_llm", "kimi/k3")
    since = phase.since
    phase.set("awaiting_llm", "kimi/k3")
    assert phase.since == since


def test_clear_resets() -> None:
    phase = TurnPhase()
    phase.set("tool_running", "shell")
    phase.note_chunk()
    phase.clear()
    assert phase.describe() == ""
    assert phase.chunk_count == 0

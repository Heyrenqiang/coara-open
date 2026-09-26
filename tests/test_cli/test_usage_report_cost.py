"""CLI 端花费更新：以事件里的内核累计值为准。

背景：web 直接读内核快照，CLI 只能靠 `llm_turn_complete` 事件。事件此前不带 cost，
CLI 的花费就停在原地不动（web 照常涨），两端对不上。现在事件带当轮 cost 与会话累计，
累计值直接对齐——本地逐轮累加会在子智能体上卷、价目变更时与内核漂移。
"""

from __future__ import annotations

from src.cli.root_shim import ForegroundCoaraShim
from src.context.window import LlmUsageSnapshot
from src.core.events import TraceEvent


def _fg() -> ForegroundCoaraShim:
    fg = ForegroundCoaraShim.__new__(ForegroundCoaraShim)
    fg.session_id = "sess-1"  # type: ignore[attr-defined]
    fg._llm_usage_snapshot = LlmUsageSnapshot()  # type: ignore[attr-defined]
    return fg


def _event(payload: dict) -> TraceEvent:
    return TraceEvent(
        coara_id="root",
        coara_name="考拉助手",
        event_type="llm_turn_complete",
        message="Model turn completed",
        payload=payload,
    )


def test_usage_report_aligns_kernel_cumulative_cost() -> None:
    fg = _fg()
    fg._on_usage_report(
        _event(
            {
                "session_id": "sess-1",
                "llm_output": {"usage": {"input_tokens": 1000, "output_tokens": 50}},
                "cost": 0.12,
                "cumulative_cost": 3.45,
            }
        )
    )

    assert fg._llm_usage_snapshot.usage["input_tokens"] == 1000
    assert fg._llm_usage_snapshot.cumulative_cost == 3.45


def test_usage_report_falls_back_to_local_accumulation() -> None:
    """老内核不带累计字段时退回逐轮累加，行为不变。"""
    fg = _fg()
    fg._on_usage_report(
        _event(
            {
                "session_id": "sess-1",
                "llm_output": {"usage": {"input_tokens": 800, "output_tokens": 20}},
                "cost": 0.5,
            }
        )
    )

    assert fg._llm_usage_snapshot.cumulative_cost == 0.5


def test_usage_report_ignores_other_sessions() -> None:
    """子智能体/他端会话的用量不得覆盖本会话累计。"""
    fg = _fg()
    fg._on_usage_report(
        _event(
            {
                "session_id": "child-sess",
                "llm_output": {"usage": {"input_tokens": 5000, "output_tokens": 10}},
                "cumulative_cost": 99.0,
            }
        )
    )

    assert fg._llm_usage_snapshot.cumulative_cost == 0.0
    assert fg._llm_usage_snapshot.usage is None

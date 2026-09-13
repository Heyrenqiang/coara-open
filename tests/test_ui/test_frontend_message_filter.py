"""Frontend isolation for L1 conversation projection hydrate (display matrix)."""

from __future__ import annotations

from pathlib import Path

from src.session_log.conversation_projection import filter_rows_for_frontend, iter_conversation_rows
from src.session_log.store import append_events
from src.session_log.types import build_event


def _event(kind: str, seq: int, *, payload: dict | None = None, turn_id: str = "") -> dict:
    return build_event(
        kind=kind,
        seq=seq,
        session_id="s1",
        payload=payload or {},
        turn_id=turn_id,
        agent_kind="main",
        ts=1_700_000_000.0,
    )


def _tape(tmp_path: Path, events: list[dict]) -> Path:
    path = tmp_path / "session_events.jsonl"
    append_events(path, events)
    return path


def test_projection_filter_keeps_matching_frontend_only(tmp_path: Path) -> None:
    tape = _tape(
        tmp_path,
        [
            _event("turn/start", 1, payload={"source": "web"}, turn_id="t-web"),
            _event("user/message", 2, payload={"content": "from web"}),
            _event("assistant/message", 3, payload={"content": "web reply"}),
            _event("turn/start", 4, payload={"source": "matrix"}, turn_id="t-mx"),
            _event("user/message", 5, payload={"content": "from phone"}),
            _event("assistant/message", 6, payload={"content": "phone reply"}),
        ],
    )
    rows = list(iter_conversation_rows(tape, include_agent_kinds={"main"}))

    web_rows = filter_rows_for_frontend(rows, "web")
    assert [r["content"] for r in web_rows] == ["from web", "web reply"]

    mx_rows = filter_rows_for_frontend(rows, "matrix")
    assert [r["content"] for r in mx_rows] == ["from phone", "phone reply"]


def test_legacy_turn_without_source_kept_on_every_frontend(tmp_path: Path) -> None:
    """turn/start 无 payload.source 的老事件：source 解析不到 → 保留（静默丢行比多显示更糟）。"""
    tape = _tape(
        tmp_path,
        [
            _event("turn/start", 1, turn_id="t1"),
            _event("user/message", 2, payload={"content": "legacy"}),
            _event("assistant/message", 3, payload={"content": "legacy reply"}),
        ],
    )
    rows = list(iter_conversation_rows(tape, include_agent_kinds={"main"}))
    assert [r["content"] for r in filter_rows_for_frontend(rows, "web")] == ["legacy", "legacy reply"]
    assert len(filter_rows_for_frontend(rows, "matrix")) == 2

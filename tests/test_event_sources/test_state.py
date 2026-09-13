"""Tests for event-source seen-key dedupe state."""

from __future__ import annotations

import json
import time

from src.event_sources.state import EventSourceStateStore


def _write_state(store: EventSourceStateStore, source_id: str, state: dict) -> None:
    (store.root / f"{source_id}.json").write_text(json.dumps(state), encoding="utf-8")


def _read_state(store: EventSourceStateStore, source_id: str) -> dict:
    return json.loads((store.root / f"{source_id}.json").read_text(encoding="utf-8"))


def test_seen_keys_dedupe_and_lifecycle(tmp_path) -> None:
    store = EventSourceStateStore(tmp_path)
    _write_state(store, "src1", {"seen_keys": [["key-a", time.time()]]})
    assert store.should_emit("src1", "key-a", cooldown_seconds=30) is False
    assert store.should_emit("src1", "key-b", cooldown_seconds=30) is True

    _write_state(store, "src1", {"seen_keys": [["key-a", time.time() - 3600]]})
    assert store.should_emit("src1", "key-a", cooldown_seconds=30) is True

    # Legacy plain list[str] must stay readable.
    _write_state(store, "src1", {"seen_keys": ["key-a"]})
    assert store.should_emit("src1", "key-a", cooldown_seconds=30) is False

    _write_state(store, "src1", {"seen_keys": [["key-a", time.time() - 3600]]})
    store.mark_emitted("src1", "key-a")
    pair = _read_state(store, "src1")["seen_keys"][0]
    assert pair[0] == "key-a" and time.time() - pair[1] < 30
    assert store.should_emit("src1", "key-a", cooldown_seconds=30) is False

    for i in range(600):
        store.mark_emitted("src1", f"key-{i}")
    capped = _read_state(store, "src1")["seen_keys"]
    assert len(capped) == 500
    assert capped[0][0] == "key-100"
    assert capped[-1][0] == "key-599"

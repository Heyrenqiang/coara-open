"""Event-source dedupe and persistent state."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from src.core.json_store import write_json_atomic

_PATH_COOLDOWN_ID = "__path_cooldown__"

_SEEN_KEYS_MAX = 500


def _load_seen_pairs(raw: Any, *, now: float) -> list[list]:
    """Normalize ``seen_keys`` to ``[[key, ts], ...]`` pairs.

    Legacy state files store a plain ``list[str]``; those entries are treated
    as seen at load time so a mid-cooldown upgrade does not re-fire duplicates.
    """
    pairs: list[list] = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                pairs.append([str(item[0]), float(item[1])])
            except (TypeError, ValueError):
                continue
        elif isinstance(item, str):
            pairs.append([item, now])
    return pairs


class EventSourceStateStore:
    """JSON state per event source: dedupe keys, poll watermarks, cooldown."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # Serializes check-and-mark sequences so concurrent duplicate events
        # cannot both pass should_emit before either reaches mark_emitted.
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def locked(self):
        """Hold the store lock across a compound check-and-mark sequence."""
        with self._lock:
            yield

    def _path(self, source_id: str) -> Path:
        safe = source_id.replace("/", "_")
        return self.root / f"{safe}.json"

    def load(self, source_id: str) -> dict[str, Any]:
        path = self._path(source_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self, source_id: str, state: dict[str, Any]) -> None:
        write_json_atomic(self._path(source_id), state)

    def should_emit(self, source_id: str, dedupe_key: str, *, cooldown_seconds: float) -> bool:
        with self._lock:
            state = self.load(source_id)
            now = time.time()
            last_emit = float(state.get("last_emit_at", 0))
            if now - last_emit < cooldown_seconds:
                return False
            cutoff = now - cooldown_seconds
            seen = {key for key, ts in _load_seen_pairs(state.get("seen_keys"), now=now) if ts >= cutoff}
            return dedupe_key not in seen

    def mark_emitted(self, source_id: str, dedupe_key: str) -> None:
        with self._lock:
            state = self.load(source_id)
            now = time.time()
            seen = _load_seen_pairs(state.get("seen_keys"), now=now)
            seen = [pair for pair in seen if pair[0] != dedupe_key]
            seen.append([dedupe_key, now])
            state["seen_keys"] = seen[-_SEEN_KEYS_MAX:]
            state["last_emit_at"] = now
            state["last_dedupe_key"] = dedupe_key
            self.save(source_id, state)

    def should_emit_path(self, path_key: str, *, cooldown_seconds: float) -> bool:
        with self._lock:
            state = self.load(_PATH_COOLDOWN_ID)
            now = time.time()
            last_by_key: dict[str, float] = dict(state.get("last_by_key") or {})
            last_emit = float(last_by_key.get(path_key, 0))
            return now - last_emit >= cooldown_seconds

    def mark_path_emitted(self, path_key: str) -> None:
        with self._lock:
            state = self.load(_PATH_COOLDOWN_ID)
            last_by_key: dict[str, float] = dict(state.get("last_by_key") or {})
            last_by_key[path_key] = time.time()
            if len(last_by_key) > 2000:
                sorted_keys = sorted(last_by_key, key=last_by_key.get)
                last_by_key = {key: last_by_key[key] for key in sorted_keys[-1500:]}
            state["last_by_key"] = last_by_key
            self.save(_PATH_COOLDOWN_ID, state)

    def poll_snapshot(self, source_id: str) -> set[str]:
        with self._lock:
            state = self.load(source_id)
            return set(state.get("poll_seen") or [])

    def save_poll_snapshot(self, source_id: str, names: set[str]) -> None:
        with self._lock:
            state = self.load(source_id)
            state["poll_seen"] = sorted(names)[-1000:]
            self.save(source_id, state)

"""Registry mapping event sources to WDL workflow triggers.

Persisted to ``<wdl_home>/triggers.json``. On event source fire, the registry
is queried for matching entries and the corresponding WDL is submitted to the
engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wdl.json_store import write_json_atomic
from wdl.logging import logger


@dataclass(slots=True)
class TriggerEntry:
    """A single WDL trigger registration."""

    workflow_name: str
    wdl_text: str
    trigger: str  # "event" | "webhook"
    event_id: str | None = None
    webhook_path: str | None = None
    webhook_secret: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "wdl_text": self.wdl_text,
            "trigger": self.trigger,
            "event_id": self.event_id,
            "webhook_path": self.webhook_path,
            "webhook_secret": self.webhook_secret,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TriggerEntry:
        return cls(
            workflow_name=str(data.get("workflow_name") or ""),
            wdl_text=str(data.get("wdl_text") or ""),
            trigger=str(data.get("trigger") or "event"),
            event_id=data.get("event_id"),
            webhook_path=data.get("webhook_path"),
            webhook_secret=data.get("webhook_secret"),
        )


class WorkflowTriggerRegistry:
    """Maps event_id → list of TriggerEntry; persisted to JSON."""

    def __init__(self, store_path: Path | str) -> None:
        self._store_path = Path(store_path)
        self._entries: list[TriggerEntry] = []

    def register(self, entry: TriggerEntry) -> None:
        # Replace existing entry with same workflow_name
        self._entries = [e for e in self._entries if e.workflow_name != entry.workflow_name]
        self._entries.append(entry)
        logger.info(f"Trigger registered: {entry.workflow_name} for event '{entry.event_id}'")

    def unregister(self, workflow_name: str) -> None:
        self._entries = [e for e in self._entries if e.workflow_name != workflow_name]

    def find_for_event(self, event_id: str) -> list[TriggerEntry]:
        return [e for e in self._entries if e.event_id == event_id]

    def load(self) -> None:
        if not self._store_path.exists():
            self._entries = []
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else data
            if isinstance(entries, list):
                self._entries = [TriggerEntry.from_dict(e) for e in entries if isinstance(e, dict)]
            else:
                self._entries = []
        except Exception as exc:
            logger.warning(f"Failed to load trigger registry: {exc}")
            self._entries = []

    def save(self) -> None:
        data = {"entries": [e.to_dict() for e in self._entries]}
        write_json_atomic(self._store_path, data)

    @property
    def entries(self) -> list[TriggerEntry]:
        return list(self._entries)

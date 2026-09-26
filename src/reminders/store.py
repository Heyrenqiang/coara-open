"""JSON persistence for scheduled reminders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.json_store import TypedJsonStore
from src.reminders.types import ReminderRecord


def _extract_reminder_items(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("reminders", [])
    else:
        return []
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return []
    return items


def _wrap_reminder_payload(serialized: list[dict[str, Any]]) -> dict[str, Any]:
    return {"reminders": serialized}


def _serialize_reminder_records(records: dict[str, ReminderRecord]) -> list[dict[str, Any]]:
    return [record.model_dump() for record in records.values()]


class ReminderStore:
    def __init__(self, path: Path):
        self.path = path
        self._inner = TypedJsonStore[ReminderRecord](
            path,
            extract_items=_extract_reminder_items,
            wrap_payload=_wrap_reminder_payload,
            parse_record=ReminderRecord.model_validate,
            serialize_records=_serialize_reminder_records,
            record_label="reminder",
        )

    def load(self) -> dict[str, ReminderRecord]:
        return self._inner.load()

    def save(self, records: dict[str, ReminderRecord]) -> bool:
        """Persist records; False when the corruption guard skipped the write"""
        return self._inner.save(records)

"""Trace events for runtime visibility and dashboard rendering."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.core.time import now_iso


@dataclass(slots=True)
class TraceEvent:
    """Structured runtime event."""

    coara_id: str
    coara_name: str
    event_type: str
    message: str
    level: str = "info"
    timestamp: str = field(default_factory=lambda: now_iso(timespec="seconds"))
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TraceEvent:
        return cls(
            coara_id=payload["coara_id"],
            coara_name=payload["coara_name"],
            event_type=payload["event_type"],
            message=payload["message"],
            level=payload.get("level", "info"),
            timestamp=payload.get("timestamp") or now_iso(timespec="seconds"),
            payload=payload.get("payload", {}),
        )

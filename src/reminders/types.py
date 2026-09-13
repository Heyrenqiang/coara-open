"""Reminder record types."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from src.core.time import now_iso


class ReminderKind(StrEnum):
    ONE_TIME = "one_time"
    INTERVAL = "interval"
    CRON = "cron"


class ReminderRecord(BaseModel):
    id: str
    kind: ReminderKind
    message: str
    created_at: str = Field(default_factory=now_iso)
    next_run_at: str
    interval_seconds: float | None = None
    cron: str | None = None
    enabled: bool = True
    # one_time 到点后的投递中标记：落盘先于投递，崩溃重启时视为已投不重发
    delivering: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "message": self.message,
            "created_at": self.created_at,
            "next_run_at": self.next_run_at,
            "interval_seconds": self.interval_seconds,
            "cron": self.cron,
            "enabled": self.enabled,
            "status": "scheduled" if self.enabled else "disabled",
        }

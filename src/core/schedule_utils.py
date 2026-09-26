"""Shared cron scheduling helpers for reminders."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def parse_cron_fields(cron: str) -> tuple[str, str, str, str, str]:
    parts = cron.strip().split()
    if len(parts) != 5:
        raise ValueError("Cron 表达式格式错误，应为 5 个字段: 分(0-59) 时(0-23) 日(1-31) 月(1-12) 星期(0-6或mon-sun)")
    return parts[0], parts[1], parts[2], parts[3], parts[4]


def compute_next_cron(cron: str, tz: ZoneInfo, after: datetime | None = None) -> datetime:
    parse_cron_fields(cron)
    base = after or datetime.now(tz)
    if base.tzinfo is None:
        base = base.replace(tzinfo=tz)
    itr = croniter(cron.strip(), base)
    nxt = itr.get_next(datetime)
    if nxt.tzinfo is None:
        return nxt.replace(tzinfo=tz)
    return nxt.astimezone(tz)

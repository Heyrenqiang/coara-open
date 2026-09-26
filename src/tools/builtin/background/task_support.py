"""Shared helpers for background task tools."""

from __future__ import annotations

from datetime import datetime

from src.background.task_store_paths import default_task_store
from src.core.time import parse_iso_to_datetime


def resolve_task_store():
    """Canonical TaskStore for background task tools in the running CLI."""
    return default_task_store()


def format_running_duration(created_at: str | None, *, now: datetime | None = None) -> str:
    """Format elapsed time for a running task (empty when not applicable)."""
    if not created_at:
        return ""
    start = parse_iso_to_datetime(created_at)
    if start is None:
        return ""
    clock = now or datetime.now()
    if start.tzinfo is not None:
        start = start.replace(tzinfo=None)
    if clock.tzinfo is not None:
        clock = clock.replace(tzinfo=None)
    secs = max(0, int((clock - start).total_seconds()))
    if secs < 60:
        return f" (已运行 {secs}s)"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f" (已运行 {mins}m {s}s)"
    hrs, m = divmod(mins, 60)
    return f" (已运行 {hrs}h {m}m)"

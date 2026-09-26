"""统一时间戳工具。

消除散落在各处的 datetime.now().isoformat() 调用，
提供统一的时间戳格式和类型转换。
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso(*, timespec: str = "auto") -> str:
    """返回本地时间的 ISO 格式字符串。"""
    return datetime.now().isoformat(timespec=timespec)


def utc_now_iso(*, timespec: str = "auto") -> str:
    """返回 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(UTC).isoformat(timespec=timespec)


def parse_utc_datetime(value: str | datetime | None) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_iso_to_datetime(value: str | datetime | None) -> datetime | None:
    """将 ISO 字符串或 datetime 对象统一转为 datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None

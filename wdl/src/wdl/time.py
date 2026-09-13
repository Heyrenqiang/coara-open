"""统一时间戳工具。"""

from __future__ import annotations

from datetime import datetime


def now_iso(*, timespec: str = "auto") -> str:
    """返回本地时间的 ISO 格式字符串。"""
    return datetime.now().isoformat(timespec=timespec)

"""Date extraction utilities for web search results."""

from __future__ import annotations

import re
from datetime import datetime, timedelta


class DateExtractor:
    """Extract and score dates from search result text."""

    _RELATIVE_EN_PATTERN = re.compile(
        r"\b(?P<value>\d+)\s+(?P<unit>minute|hour|day|week|month|year)s?\s+ago\b",
        re.IGNORECASE,
    )
    _RELATIVE_ZH_PATTERN = re.compile(
        r"(?P<value>\d+)\s*(?P<unit>分钟|小时|天|周|个月|月|年)前",
        re.IGNORECASE,
    )
    _ABSOLUTE_YMD_PATTERNS = [
        re.compile(r"(?P<year>20\d{2})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})"),
        re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"),
    ]
    _ABSOLUTE_EN_MONTH_PATTERN = re.compile(
        r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+(?P<day>\d{1,2}),?\s+(?P<year>20\d{2})\b",
        re.IGNORECASE,
    )
    _MONTH_NAME_TO_NUMBER = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }

    def extract(self, *parts: str) -> tuple[datetime | None, str, float]:
        text = " ".join(part for part in parts if part).strip()
        if not text:
            return None, "", 0.0
        relative = self._extract_relative(text)
        if relative is not None:
            return relative, relative.date().isoformat(), 0.85
        absolute = self._extract_absolute(text)
        if absolute is not None:
            return absolute, absolute.date().isoformat(), 0.95
        return None, "", 0.0

    def _extract_relative(self, text: str) -> datetime | None:
        lowered = text.lower()
        now = datetime.now()
        if any(token in lowered for token in ("just now", "moments ago")) or "刚刚" in text:
            return now
        if "today" in lowered or "今天" in text:
            return now
        if "yesterday" in lowered or "昨天" in text:
            return now - timedelta(days=1)
        match = self._RELATIVE_EN_PATTERN.search(text)
        if match:
            value = int(match.group("value"))
            unit = match.group("unit").lower()
            delta_map = {
                "minute": timedelta(minutes=value),
                "hour": timedelta(hours=value),
                "day": timedelta(days=value),
                "week": timedelta(weeks=value),
                "month": timedelta(days=value * 30),
                "year": timedelta(days=value * 365),
            }
            return now - delta_map[unit]
        match = self._RELATIVE_ZH_PATTERN.search(text)
        if match:
            value = int(match.group("value"))
            unit = match.group("unit")
            delta_map = {
                "分钟": timedelta(minutes=value),
                "小时": timedelta(hours=value),
                "天": timedelta(days=value),
                "周": timedelta(weeks=value),
                "个月": timedelta(days=value * 30),
                "月": timedelta(days=value * 30),
                "年": timedelta(days=value * 365),
            }
            return now - delta_map[unit]
        return None

    def _extract_absolute(self, text: str) -> datetime | None:
        for pattern in self._ABSOLUTE_YMD_PATTERNS:
            match = pattern.search(text)
            if match:
                try:
                    return datetime(
                        int(match.group("year")),
                        int(match.group("month")),
                        int(match.group("day")),
                    )
                except ValueError:
                    continue
        match = self._ABSOLUTE_EN_MONTH_PATTERN.search(text)
        if match:
            month_text = match.group("month").lower()
            month = self._MONTH_NAME_TO_NUMBER.get(month_text)
            if month is None:
                return None
            try:
                return datetime(
                    int(match.group("year")),
                    month,
                    int(match.group("day")),
                )
            except ValueError:
                return None
        return None

    @staticmethod
    def freshness_score(date_iso: str, time_window_days: int = 30) -> float:
        date_iso = date_iso.strip()
        if not date_iso:
            return 0.0
        try:
            observed_at = datetime.fromisoformat(date_iso)
        except ValueError:
            return 0.0
        age_days = max(0.0, (datetime.now() - observed_at).total_seconds() / 86400)
        window = max(1, time_window_days)
        return max(0.0, 1.0 - (age_days / window))

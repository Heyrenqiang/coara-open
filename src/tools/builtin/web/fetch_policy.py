"""web_fetch session policy: dedup, limits, and content quality scoring."""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from src.core.config import config_manager

_MAIN_CONTENT_PATTERNS = (
    re.compile(r"<article\b[^>]*>(.*?)</article>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<main\b[^>]*>(.*?)</main>", re.IGNORECASE | re.DOTALL),
    re.compile(r'<div\b[^>]*\brole=["\']main["\'][^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL),
)
_BLOCK_BREAK_RE = re.compile(r"</(?:p|div|section|article|main|h[1-6]|li|tr|br)\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)>", re.IGNORECASE | re.DOTALL
)
_NAV_ASIDE_RE = re.compile(
    r"<(?:nav|aside|header|footer)\b[^>]*>.*?</(?:nav|aside|header|footer)>", re.IGNORECASE | re.DOTALL
)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class FetchPolicyConfig:
    enabled: bool = True
    max_per_session: int = 25
    max_per_domain: int = 6
    min_quality_score: float = 0.18
    skip_duplicate_url: bool = True
    min_meaningful_chars: int = 120


@dataclass
class _SessionFetchState:
    total: int = 0
    by_domain: dict[str, int] = field(default_factory=dict)
    urls: set[str] = field(default_factory=set)


class FetchSessionTracker:
    """In-memory per-session fetch counters (cleared when sessions are evicted)."""

    _lock = asyncio.Lock()
    # Ordered by last access: eviction drops the least-recently-used session.
    _sessions: OrderedDict[str, _SessionFetchState] = OrderedDict()
    _max_sessions = 128

    @classmethod
    async def _state(cls, session_id: str) -> _SessionFetchState:
        async with cls._lock:
            state = cls._sessions.get(session_id)
            if state is not None:
                cls._sessions.move_to_end(session_id)
                return state
            if len(cls._sessions) >= cls._max_sessions:
                oldest = next(iter(cls._sessions))
                cls._sessions.pop(oldest, None)
            state = _SessionFetchState()
            cls._sessions[session_id] = state
            return state

    @classmethod
    async def check(cls, session_id: str, url: str, policy: FetchPolicyConfig) -> str | None:
        if not policy.enabled or not session_id:
            return None
        domain = domain_from_url(url)
        state = await cls._state(session_id)
        async with cls._lock:
            if policy.skip_duplicate_url and url in state.urls:
                return "duplicate_url"
            if policy.max_per_session > 0 and state.total >= policy.max_per_session:
                return "session_limit"
            if policy.max_per_domain > 0 and domain and state.by_domain.get(domain, 0) >= policy.max_per_domain:
                return "domain_limit"
            return None

    @classmethod
    async def record(cls, session_id: str, url: str) -> None:
        if not session_id:
            return
        domain = domain_from_url(url)
        async with cls._lock:
            state = cls._sessions.setdefault(session_id, _SessionFetchState())
            cls._sessions.move_to_end(session_id)
            state.total += 1
            state.urls.add(url)
            if domain:
                state.by_domain[domain] = state.by_domain.get(domain, 0) + 1


def load_fetch_policy_config(raw_config: dict[str, Any] | None = None) -> FetchPolicyConfig:
    raw = raw_config
    if raw is None and config_manager._config is not None:
        raw = getattr(config_manager, "_raw_config", {}) or {}
    section = (raw or {}).get("web_fetch") or {}
    return FetchPolicyConfig(
        enabled=bool(section.get("enabled", True)),
        max_per_session=int(section.get("max_per_session", 25) or 25),
        max_per_domain=int(section.get("max_per_domain", 6) or 6),
        min_quality_score=float(section.get("min_quality_score", 0.18) or 0.18),
        skip_duplicate_url=bool(section.get("skip_duplicate_url", True)),
        min_meaningful_chars=int(section.get("min_meaningful_chars", 120) or 120),
    )


def block_reason_message(reason: str, *, url: str, policy: FetchPolicyConfig) -> str:
    if reason == "duplicate_url":
        return (
            f"Skipped duplicate fetch for '{url}' — this URL was already fetched in the current session. "
            "Use earlier tool results or pick another source."
        )
    if reason == "session_limit":
        return (
            f"Session fetch limit reached ({policy.max_per_session}). "
            "Prefer web_search snippets or delegate deeper research."
        )
    if reason == "domain_limit":
        domain = domain_from_url(url) or "this domain"
        return (
            f"Domain fetch limit reached for {domain} ({policy.max_per_domain} per session). "
            "Try another site or use existing results."
        )
    return f"Fetch blocked ({reason})."


def score_extracted_text(text: str) -> float:
    stripped = text.strip()
    if not stripped:
        return 0.0
    length = len(stripped)
    if length < 80:
        return 0.05
    words = stripped.split()
    word_count = len(words)
    char_estimate = length // 3
    effective_words = max(word_count, char_estimate)
    if effective_words < 25:
        return 0.12

    alpha = sum(ch.isalnum() for ch in stripped)
    alpha_ratio = alpha / max(length, 1)
    if alpha_ratio < 0.45:
        return 0.15

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if lines:
        unique_ratio = len(set(lines)) / len(lines)
        avg_len = sum(len(line) for line in lines) / len(lines)
        if unique_ratio < 0.35 and avg_len < 36:
            return 0.18

    score = 0.25 + min(alpha_ratio, 0.95) * 0.45 + min(effective_words, 800) / 1600
    return min(1.0, score)


def extract_readable_text(html: str) -> str:
    """Prefer trafilatura for high-quality extraction, fallback to regex."""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        if extracted and len(extracted.strip()) >= 120:
            return extracted.strip()
    except Exception:
        pass

    markup = _SCRIPT_STYLE_RE.sub(" ", html)
    markup = _NAV_ASIDE_RE.sub(" ", markup)
    for pattern in _MAIN_CONTENT_PATTERNS:
        match = pattern.search(markup)
        if match and len(match.group(1)) >= 400:
            markup = match.group(1)
            break
    markup = _BLOCK_BREAK_RE.sub("\n", markup)
    without_tags = _TAG_RE.sub(" ", markup)
    lines = []
    for raw_line in without_tags.splitlines():
        line = _WS_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def low_quality_message(*, url: str, score: float, chars: int, policy: FetchPolicyConfig) -> str:
    return (
        f"Low-value page content for '{url}' (quality={score:.2f}, {chars} chars). "
        f"Minimum quality is {policy.min_quality_score:.2f}. "
        "Prefer web_search snippets or another URL."
    )


def build_fetch_meta(
    *,
    url: str,
    chars: int,
    quality: float,
    blocked: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "url": url,
        "chars": chars,
        "quality": round(quality, 3),
    }
    if blocked:
        meta["blocked"] = blocked
    return meta


def domain_from_url(url: str) -> str:
    """Registrable domain (last two labels) for policy limits and blocklists."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return ""
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3:
        return ".".join(parts[-2:])
    return host


__all__ = [
    "FetchPolicyConfig",
    "FetchSessionTracker",
    "block_reason_message",
    "build_fetch_meta",
    "domain_from_url",
    "extract_readable_text",
    "load_fetch_policy_config",
    "low_quality_message",
    "score_extracted_text",
]

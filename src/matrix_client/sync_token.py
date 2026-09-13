"""GoMatrix-compatible Matrix /sync token helpers."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from src.core.json_store import write_text_atomic


def normalize_gomatrix_sync_token(raw: str | None) -> str | None:
    """Return token when valid for GoMatrix (``s<number>``), else None."""
    if not raw:
        return None
    token = raw.strip()
    if len(token) > 1 and token.startswith("s") and token[1:].isdigit():
        return token
    return None


def load_gomatrix_sync_token(path: Path) -> str | None:
    """Load sync token from disk; delete invalid tokens."""
    if not path.is_file():
        return None
    saved = path.read_text(encoding="utf-8").strip()
    normalized = normalize_gomatrix_sync_token(saved)
    if normalized:
        return normalized
    with suppress(OSError):
        path.unlink()
    return None


def save_gomatrix_sync_token(path: Path, token: str | None) -> None:
    """Persist a GoMatrix sync token; ignore invalid values."""
    normalized = normalize_gomatrix_sync_token(token)
    if not normalized:
        return
    write_text_atomic(path, normalized)


def reset_gomatrix_sync_token(client: object, path: Path) -> None:
    """Clear in-memory and on-disk sync state after token errors."""
    if hasattr(client, "next_batch"):
        client.next_batch = None
    with suppress(OSError):
        path.unlink(missing_ok=True)


def is_invalid_sync_token_error(err: object) -> bool:
    text = str(err).lower()
    return "invalid sync token" in text or ("m_invalid_param" in text and "sync" in text)

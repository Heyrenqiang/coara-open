"""Debug helpers shared across LLM providers."""

from __future__ import annotations

from typing import Any

_MAX_STRING = 200
# Keys whose string values are binary/base64 payloads — cap them harder so
# image data never floods DEBUG logs.
_PAYLOAD_KEYS = {"data", "image", "image_url", "source", "base64"}
_MAX_PAYLOAD_STRING = 64


def _truncate_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        limit = _MAX_PAYLOAD_STRING if key in _PAYLOAD_KEYS else _MAX_STRING
        if len(value) > limit:
            return value[:limit] + "..."
        return value
    if isinstance(value, dict):
        return {k: _truncate_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_value("", item) for item in value]
    return value


def truncate_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *d* with long strings truncated for logging.

    Recurses into nested dicts/lists; the input is never mutated.
    """
    return {k: _truncate_value(k, v) for k, v in d.items()}

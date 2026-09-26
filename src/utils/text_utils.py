"""Shared text helpers for CLI, LLM payloads, and gateways."""

from __future__ import annotations

from typing import Any


def sanitize_surrogates(text: str) -> str:
    """Make text UTF-8 safe: recombine Windows UTF-16 pairs, drop lone surrogates.

    Windows console / prompt_toolkit often yields emoji as two UTF-16 code units
    (e.g. ``\\ud83d\\udc4d``). Those are legal in Python ``str`` but cannot
    be encoded as UTF-8, so WebSocket / JSON send blows up. Paired surrogates are
    recombined into a real code point; unpaired ones are dropped.
    """
    if not text or not any("\ud800" <= c <= "\udfff" for c in text):
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        o = ord(text[i])
        if 0xD800 <= o <= 0xDBFF and i + 1 < n:
            o2 = ord(text[i + 1])
            if 0xDC00 <= o2 <= 0xDFFF:
                code = 0x10000 + ((o - 0xD800) << 10) + (o2 - 0xDC00)
                out.append(chr(code))
                i += 2
                continue
        if 0xD800 <= o <= 0xDFFF:
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def sanitize_json_payload(value: Any) -> Any:
    """Recursively sanitize strings inside nested JSON-serializable structures."""
    if isinstance(value, str):
        return sanitize_surrogates(value)
    if isinstance(value, list):
        return [sanitize_json_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_json_payload(item) for key, item in value.items()}
    return value

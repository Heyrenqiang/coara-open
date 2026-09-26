"""sanitize_surrogates: Windows UTF-16 pairs → real code points; drop loners."""

from __future__ import annotations

import json

from src.utils.text_utils import sanitize_json_payload, sanitize_surrogates


def test_recombines_emoji_surrogate_pair() -> None:
    # 👍 as Windows/console UTF-16 high+low surrogates
    dirty = "\ud83d\udc4d"
    assert sanitize_surrogates(dirty) == "👍"


def test_mixed_text_with_emoji_pair() -> None:
    dirty = "好的\ud83d\udc4d收到"
    assert sanitize_surrogates(dirty) == "好的👍收到"


def test_strips_lone_surrogate() -> None:
    dirty = "分析\ud800报告"
    assert sanitize_surrogates(dirty) == "分析报告"


def test_already_clean_emoji_unchanged() -> None:
    assert sanitize_surrogates("👍") == "👍"


def test_json_payload_and_utf8_encode() -> None:
    frame = {"type": "pending_report", "text": "\ud83d\udc4d"}
    safe = sanitize_json_payload(frame)
    assert safe["text"] == "👍"
    # Must survive the same encode path aiohttp send_str uses
    json.dumps(safe, ensure_ascii=False).encode("utf-8")

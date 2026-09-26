"""Tests for llm._debug.truncate_dict."""

from __future__ import annotations

from src.llm._debug import truncate_dict


def test_truncates_long_strings_and_marks_ellipsis() -> None:
    result = truncate_dict({"text": "x" * 300})
    assert result["text"] == "x" * 200 + "..."


def test_short_strings_not_marked() -> None:
    # Regression: short text used to get an unconditional "..." appended.
    result = truncate_dict({"text": "short"})
    assert result["text"] == "short"


def test_recurses_into_nested_image_payloads() -> None:
    payload = "a" * 5000
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}},
        ],
    }
    result = truncate_dict(message)
    source = result["content"][1]["source"]
    assert source["data"] == "a" * 64 + "..."
    assert source["media_type"] == "image/png"
    assert result["content"][0]["text"] == "hello"
    # Pure function: the input is left untouched.
    assert message["content"][1]["source"]["data"] == payload


def test_tool_result_blocks_keep_shape_with_truncation() -> None:
    message = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tc-1",
                "content": "y" * 500,
            }
        ],
    }
    result = truncate_dict(message)
    block = result["content"][0]
    assert block["tool_use_id"] == "tc-1"
    assert block["content"] == "y" * 200 + "..."

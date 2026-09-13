"""Tokenizer / Message must tolerate lone Unicode surrogates."""

from __future__ import annotations

from src.core.types import Message, MessageRole
from src.llm.tokenizer import estimate_tokens


def test_estimate_tokens_strips_lone_surrogates() -> None:
    # Lone high surrogate — valid in Python str, invalid for UTF-8 / tokenizers.
    dirty = "hello\ud800world"
    assert estimate_tokens(dirty) >= 1
    assert estimate_tokens("plain ascii") >= 1


def test_message_sanitizes_surrogates_on_construct() -> None:
    msg = Message(role=MessageRole.USER, content="分析\ud800报告")
    assert "\ud800" not in msg.content
    assert "分析" in msg.content
    assert "报告" in msg.content

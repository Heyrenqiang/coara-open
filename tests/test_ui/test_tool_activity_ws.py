"""Tests for Web tool-activity WS field normalization (tool_start)."""

from __future__ import annotations

from src.ui.web_server import WebServer


def test_normalize_tool_start_fields() -> None:
    target: dict = {"type": "tool_start"}
    source = {
        "tool_name": "media",
        "tool_call_id": "abc123",
        "arguments": {"kind": "image"},
    }
    WebServer._normalize_tool_ws_fields("tool_start", target, source)
    assert target["tool"] == "media"
    assert target["call_id"] == "abc123"
    assert target["args"] == {"kind": "image"}


def test_normalize_tool_call_keeps_existing_tool() -> None:
    target: dict = {"type": "tool_call", "tool": "shell", "call_id": "x"}
    WebServer._normalize_tool_ws_fields("tool_call", target, {"tool_name": "other", "tool_call_id": "y"})
    assert target["tool"] == "shell"
    assert target["call_id"] == "x"


def test_normalize_ignores_non_tool_events() -> None:
    target: dict = {"type": "chat_chunk"}
    WebServer._normalize_tool_ws_fields("chat_chunk", target, {"tool_name": "x"})
    assert "tool" not in target

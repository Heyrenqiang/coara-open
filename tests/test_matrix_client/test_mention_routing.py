"""Tests for @mention routing."""

from __future__ import annotations

from src.matrix_client.mention_routing import (
    AgentDescriptor,
    parse_mention,
    should_process,
)


def test_parse_mention_strips_domain() -> None:
    assert parse_mention("@coara:coara.local hello") == "coara"


def test_should_process_full_mxid_mention() -> None:
    agents = [
        AgentDescriptor(name="coara", user_id="@coara:coara.local", is_default=True),
        AgentDescriptor(name="bob", user_id="@bob:coara.local", is_default=False),
    ]
    route = should_process("@coara:coara.local 你好", "coara", agents)
    assert route.should_process
    assert route.body == "你好"

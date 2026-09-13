"""Slash /records and /collect are retired — use Web/手机手点与 local_search."""

from __future__ import annotations

from src.coara.commands.registry import _HANDLERS


def test_records_and_collect_slash_unregistered() -> None:
    assert "records" not in _HANDLERS
    assert "collect" not in _HANDLERS

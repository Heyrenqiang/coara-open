"""Tests for Web slash picker payload (CLI parity)."""

from __future__ import annotations

from types import SimpleNamespace

from src.cli.slash_pickers import (
    pickers_payload_for_root,
)


def test_pickers_payload_includes_static_and_submit_flags() -> None:
    root = SimpleNamespace(
        workspace_manager=None,
        foreground_coara=SimpleNamespace(
            provider_name="openai",
            model_name="gpt",
            workspace_dir=".",
        ),
        sync_workspace_manager_to_foreground=lambda: None,
    )
    payload = pickers_payload_for_root(root)
    assert "/model" in payload
    assert "/ws" in payload
    thinking = payload["/thinking"]
    assert all(row["submit"] is True for row in thinking)
    assert all("display" in row and "meta" in row for row in thinking)

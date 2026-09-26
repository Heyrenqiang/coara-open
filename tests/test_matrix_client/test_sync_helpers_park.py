"""Tests for Matrix sync catch-up helpers that prevent history replay."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.matrix_client.sync_helpers import park_sync_token_without_timeline
from src.matrix_client.sync_token import load_gomatrix_sync_token


@pytest.mark.asyncio
async def test_park_sync_token_without_timeline_persists_batch(tmp_path: Path) -> None:
    token_path = tmp_path / "matrix_sync_token_cli"
    client = SimpleNamespace(next_batch=None)
    client.sync = AsyncMock(return_value=SimpleNamespace(next_batch="s42"))

    async def _sync(**_kwargs):
        client.next_batch = "s42"
        return SimpleNamespace(next_batch="s42")

    client.sync = _sync

    parked = await park_sync_token_without_timeline(client, token_path=token_path, label="test")
    assert parked == "s42"
    assert load_gomatrix_sync_token(token_path) == "s42"


@pytest.mark.asyncio
async def test_park_sync_token_without_timeline_handles_error(tmp_path: Path) -> None:
    token_path = tmp_path / "matrix_sync_token_cli"
    client = SimpleNamespace(next_batch=None)

    async def _sync(**_kwargs):
        return RuntimeError("boom")

    client.sync = _sync
    assert await park_sync_token_without_timeline(client, token_path=token_path) is None
    assert not token_path.exists()

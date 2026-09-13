"""Tests for Matrix pending-interaction persistence and restart invalidation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import src.matrix_client.pending_interactions as pi
from src.matrix_client.pending_interactions import (
    KIND_VAULT,
    discard_pending_interaction,
    load_pending_interactions,
    notify_stale_pending_interactions,
    record_pending_interaction,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path):
    pi.configure_pending_interactions_store(tmp_path / "pending.json")
    pi._RECORDS.clear()  # noqa: SLF001
    yield
    pi._RECORDS.clear()  # noqa: SLF001
    pi.configure_pending_interactions_store(None)


def _store_path(tmp_path) -> Path:
    path = pi.pending_interactions_store_path()
    assert path is not None
    return path


def test_record_persists_and_loads_roundtrip(tmp_path) -> None:
    record_pending_interaction(KIND_VAULT, "!room:local", ttl_seconds=300.0)
    path = _store_path(tmp_path)
    assert path.exists()
    records = load_pending_interactions()
    assert len(records) == 1
    rec = records[0]
    assert rec.kind == KIND_VAULT
    assert rec.room_id == "!room:local"
    assert rec.expires_at > rec.created_at


def test_discard_removes_record_from_disk(tmp_path) -> None:
    record_pending_interaction(KIND_VAULT, "!room:local", ttl_seconds=300.0)
    discard_pending_interaction(KIND_VAULT, "!room:local")
    assert load_pending_interactions() == []
    payload = json.loads(_store_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["pending"] == []


def test_record_is_noop_without_store_path(monkeypatch) -> None:
    pi.configure_pending_interactions_store(None)
    from src.core.config import config_manager

    monkeypatch.setattr(config_manager, "_config", None)
    record_pending_interaction(KIND_VAULT, "!room:local", ttl_seconds=60.0)  # must not raise
    discard_pending_interaction(KIND_VAULT, "!room:local")  # must not raise


def test_load_tolerates_corrupt_file(tmp_path) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json{", encoding="utf-8")
    assert load_pending_interactions() == []


async def test_sweep_notifies_unexpired_and_clears_file(tmp_path) -> None:
    now = time.time()
    record_pending_interaction(KIND_VAULT, "!room:local", ttl_seconds=300.0)
    # Inject an expired record straight into the file.
    path = _store_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pending"].append(
        {
            "kind": KIND_VAULT,
            "room_id": "!old:local",
            "created_at": now - 7200.0,
            "expires_at": now - 3600.0,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    sent: list[tuple[str, str]] = []

    async def _send(room_id: str, body: str) -> bool:
        sent.append((room_id, body))
        return True

    notified = await notify_stale_pending_interactions(_send, now=now)
    assert notified == 1
    assert len(sent) == 1
    assert sent[0][0] == "!room:local"
    assert "已重启" in sent[0][1] and "已失效" in sent[0][1]
    # File cleared — a second sweep must not notify again.
    assert not path.exists()
    assert await notify_stale_pending_interactions(_send, now=now) == 0


async def test_sweep_send_failure_still_clears_file(tmp_path) -> None:
    record_pending_interaction(KIND_VAULT, "!room:local", ttl_seconds=300.0)

    async def _failing_send(room_id: str, body: str) -> bool:
        raise RuntimeError("network down")

    notified = await notify_stale_pending_interactions(_failing_send)
    assert notified == 0
    assert not _store_path(tmp_path).exists()


async def test_sweep_no_file_returns_zero() -> None:
    async def _send(room_id: str, body: str) -> bool:
        return True

    assert await notify_stale_pending_interactions(_send) == 0

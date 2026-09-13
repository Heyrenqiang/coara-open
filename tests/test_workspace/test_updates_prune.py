"""Tests for workspace updates pruning."""

from __future__ import annotations

from src.workspace.updates.store import WorkspaceUpdatesStore


def _tick():
    """Deterministic, strictly increasing created_at timestamps."""
    return iter(f"2026-01-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}" for i in range(100000))


def _fill(store: WorkspaceUpdatesStore, workspace: str, count: int, *, key_prefix: str, clock) -> None:
    for i in range(count):
        msg = store.append(
            workspace=workspace,
            source_id="test",
            event_type="test.event",
            dedupe_key=f"{key_prefix}-{i}",
            text=f"message {key_prefix}-{i}",
            payload={},
        )
        assert msg is not None
        msg.created_at = next(clock)
        store._save(msg)


def test_prune_preserves_unread_messages(tmp_path, monkeypatch) -> None:
    """Second prune round must skip unread messages, even if disk exceeds the cap."""
    monkeypatch.setattr(WorkspaceUpdatesStore, "_MAX_MESSAGES_PER_WORKSPACE", 5)
    clock = _tick()
    store = WorkspaceUpdatesStore(tmp_path)
    _fill(store, "ws", 5, key_prefix="old", clock=clock)
    for msg in store.list_messages(workspace="ws", status="all", limit=100):
        store.mark_read(msg.message_id)

    _fill(store, "ws", 8, key_prefix="new", clock=clock)

    remaining = store.list_messages(workspace="ws", status="all", limit=100)
    unread = [m for m in remaining if m.status == "unread"]
    assert len(unread) == 8
    assert len(remaining) > 5


def test_prune_all_unread_may_exceed_soft_cap(tmp_path, monkeypatch) -> None:
    """When every message is unread, soft cap is soft — nothing is deleted."""
    monkeypatch.setattr(WorkspaceUpdatesStore, "_MAX_MESSAGES_PER_WORKSPACE", 3)
    clock = _tick()
    store = WorkspaceUpdatesStore(tmp_path)
    _fill(store, "ws", 7, key_prefix="u", clock=clock)
    remaining = store.list_messages(workspace="ws", status="all", limit=100)
    assert len(remaining) == 7
    assert all(m.status == "unread" for m in remaining)


def test_prune_deletes_oldest_read_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(WorkspaceUpdatesStore, "_MAX_MESSAGES_PER_WORKSPACE", 5)
    clock = _tick()
    store = WorkspaceUpdatesStore(tmp_path)
    _fill(store, "ws", 5, key_prefix="old", clock=clock)
    for msg in store.list_messages(workspace="ws", status="all", limit=100):
        store.mark_read(msg.message_id)

    _fill(store, "ws", 2, key_prefix="new", clock=clock)

    remaining_keys = {m.dedupe_key for m in store.list_messages(workspace="ws", status="all", limit=100)}
    assert len(remaining_keys) == 5
    assert "new-0" in remaining_keys
    assert "new-1" in remaining_keys
    assert "old-0" not in remaining_keys  # oldest read pruned first
    assert "old-1" not in remaining_keys

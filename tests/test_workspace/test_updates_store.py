"""Tests for typed workspace updates entries, read markers, and catalog payload."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.workspace.types import WorkspaceEntry
from src.workspace.updates.catalog import build_updates_workspaces_payload
from src.workspace.updates.store import WorkspaceUpdatesStore
from src.workspace.updates.types import WorkspaceUpdate


def _append(store: WorkspaceUpdatesStore, workspace: str, key: str, **kwargs) -> WorkspaceUpdate:
    msg = store.append(
        workspace=workspace,
        source_id="test",
        event_type="test.event",
        dedupe_key=key,
        text=f"text {key}",
        payload={},
        **kwargs,
    )
    assert msg is not None
    return msg


def _set_created_at(store: WorkspaceUpdatesStore, msg: WorkspaceUpdate, created_at: str) -> None:
    msg.created_at = created_at
    store._save(msg)


def _fake_registry(entries: dict[str, WorkspaceEntry]) -> SimpleNamespace:
    """Registry stub：resolve_name_or_id 按 name/id 查，list_active 返回全部。"""
    return SimpleNamespace(
        resolve_name_or_id=lambda ident: next((e for e in entries.values() if e.name == ident or e.id == ident), None),
        list_active=lambda: list(entries.values()),
    )


def test_registry_layout_writes_to_workspace_inbox(tmp_path) -> None:
    """有 registry 时动态落 <ws>/.coara/inbox/，marker 同目录。"""
    ws_dir = tmp_path / "ws-shop"
    ws_dir.mkdir()
    entry = WorkspaceEntry(id="ws-1", name="shop", path=str(ws_dir))
    store = WorkspaceUpdatesStore(tmp_path, registry=_fake_registry({"ws-1": entry}))
    msg = _append(store, "shop", "k-reg")
    assert (ws_dir / ".coara" / "inbox" / f"{msg.message_id}.json").is_file()
    # marker 落在新布局
    store.mark_all_read("shop")
    assert (ws_dir / ".coara" / "inbox" / "read_marker.json").is_file()
    # 读回与跨空间聚合
    assert store.get(msg.message_id) is not None
    assert store.unread_count("shop") == 0
    assert store.latest("shop").message_id == msg.message_id


def test_migrate_legacy_inbox_moves_to_workspace(tmp_path) -> None:
    """旧集中布局 → 各空间 .coara/inbox/：条目与 marker 迁移，幂等。"""
    ws_dir = tmp_path / "ws-shop"
    ws_dir.mkdir()
    entry = WorkspaceEntry(id="ws-1", name="shop", path=str(ws_dir))
    # 先用无 registry store 在旧布局写一条
    legacy_store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(legacy_store, "shop", "k-legacy")
    legacy_store.mark_all_read("shop")
    old_dir = tmp_path / "users" / "default" / "inbox" / "shop"
    assert (old_dir / f"{msg.message_id}.json").is_file()
    assert (tmp_path / "users" / "default" / "inbox" / "shop.read_marker.json").is_file()

    # 带 registry 的 store 迁移
    store = WorkspaceUpdatesStore(tmp_path, registry=_fake_registry({"ws-1": entry}))
    store.migrate_legacy_inbox()
    assert (ws_dir / ".coara" / "inbox" / f"{msg.message_id}.json").is_file()
    assert (ws_dir / ".coara" / "inbox" / "read_marker.json").is_file()
    assert not old_dir.exists()
    # 幂等：再跑一次不报错
    store.migrate_legacy_inbox()


def test_registry_layout_pending_and_stats_cross_workspace(tmp_path) -> None:
    """跨空间聚合（pending/stats）遍历各空间 .coara/inbox。"""
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    reg = _fake_registry(
        {
            "a": WorkspaceEntry(id="a", name="alpha", path=str(ws_a)),
            "b": WorkspaceEntry(id="b", name="beta", path=str(ws_b)),
        }
    )
    store = WorkspaceUpdatesStore(tmp_path, registry=reg)
    high = _append(store, "alpha", "k-pa", salience="high")
    _append(store, "beta", "k-pb")
    assert {m.message_id for m in store.pending()} == {high.message_id}
    stats = store.stats()
    assert stats["alpha"]["total"] == 1
    assert stats["beta"]["total"] == 1


def test_typed_entry_defaults(tmp_path) -> None:
    """append 不传 type/payload_ref 时落盘为 note / None。"""
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k1")
    assert msg.type == "note"
    assert msg.payload_ref is None

    loaded = store.get(msg.message_id)
    assert loaded is not None
    assert loaded.type == "note"
    assert loaded.payload_ref is None


def test_typed_entry_persisted(tmp_path) -> None:
    """type/payload_ref 写入 JSON 并能读回。"""
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k2", type="file_change", payload_ref="inbox/a.json")

    raw = json.loads(store._message_path("shop", msg.message_id).read_text(encoding="utf-8"))
    assert raw["type"] == "file_change"
    assert raw["payload_ref"] == "inbox/a.json"

    loaded = WorkspaceUpdatesStore(tmp_path).get(msg.message_id)
    assert loaded is not None
    assert loaded.type == "file_change"
    assert loaded.payload_ref == "inbox/a.json"


def test_legacy_entry_without_type_fields(tmp_path) -> None:
    """既有动态 JSON 没有 type/payload_ref 时读取走 schema 默认值。"""
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k3")

    path = store._message_path("shop", msg.message_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["type"]
    del raw["payload_ref"]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = WorkspaceUpdatesStore(tmp_path).get(msg.message_id)
    assert loaded is not None
    assert loaded.type == "note"
    assert loaded.payload_ref is None


def test_unread_count_and_mark_all_read(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    older = _append(store, "shop", "old")
    _set_created_at(store, older, "2026-01-01T00:00:00")
    newer = _append(store, "shop", "new")
    _set_created_at(store, newer, "2026-01-02T00:00:00")

    assert store.unread_count("shop") == 2

    store.mark_all_read("shop")
    assert store.unread_count("shop") == 0
    assert store.read_marker("shop") == "2026-01-02T00:00:00"

    fresh = _append(store, "shop", "fresh")
    _set_created_at(store, fresh, "2026-01-03T00:00:00")
    assert store.unread_count("shop") == 1

    # 已读水位线按工作空间持久化：新 store 实例仍生效
    reloaded = WorkspaceUpdatesStore(tmp_path)
    assert reloaded.read_marker("shop") == "2026-01-02T00:00:00"
    assert reloaded.unread_count("shop") == 1

    # 归档条目不计入未读
    store.archive(fresh.message_id)
    assert WorkspaceUpdatesStore(tmp_path).unread_count("shop") == 0

    # 空工作空间推进水位线不报错
    store.mark_all_read("empty")
    assert store.unread_count("empty") == 0


def test_archive_releases_dedupe_key_for_redelivery(tmp_path) -> None:
    """Archived updates must not keep their dedupe_key in the in-memory index."""
    store = WorkspaceUpdatesStore(tmp_path)
    first = _append(store, "shop", "evt-1")
    assert (
        store.append(
            workspace="shop",
            source_id="test",
            event_type="test.event",
            dedupe_key="evt-1",
            text="dup",
            payload={},
        )
        is None
    )

    archived = store.archive(first.message_id)
    assert archived is not None
    assert archived.status == "archived"

    again = store.append(
        workspace="shop",
        source_id="test",
        event_type="test.event",
        dedupe_key="evt-1",
        text="redelivered",
        payload={},
    )
    assert again is not None
    assert again.message_id != first.message_id
    assert again.text == "redelivered"


def test_catalog_payload_includes_unread_and_latest(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    store = WorkspaceUpdatesStore(home)
    first = _append(store, "shop", "c1", type="file_change", payload_ref="inbox/a.json")
    _set_created_at(store, first, "2026-01-01T00:00:00")
    second = _append(store, "shop", "c2", type="webhook", payload_ref="fb-7")
    _set_created_at(store, second, "2026-01-02T00:00:00")

    entry = WorkspaceEntry(name="shop", id="ws-shop", path=str(tmp_path / "shop"))
    fake_registry = SimpleNamespace(
        document=SimpleNamespace(workspaces={"ws-shop": entry}),
        load=lambda: None,
    )
    monkeypatch.setattr(
        "src.workspace.updates.catalog.open_registry",
        lambda *args, **kwargs: fake_registry,
    )

    payload = build_updates_workspaces_payload(coara_home=home, workspace_dir=tmp_path)
    assert len(payload) == 1
    row = payload[0]
    assert row["name"] == "shop"
    assert row["unread"] == 2
    assert row["latest"] == {
        "type": "webhook",
        "title": second.title,
        "created_at": "2026-01-02T00:00:00",
    }

    # 全部已读后红点清零，latest 仍保留
    store.mark_all_read("shop")
    payload = build_updates_workspaces_payload(coara_home=home, workspace_dir=tmp_path)
    assert payload[0]["unread"] == 0
    assert payload[0]["latest"]["type"] == "webhook"


def test_catalog_payload_no_entries(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    entry = WorkspaceEntry(name="quiet", id="ws-quiet", path=str(tmp_path / "quiet"))
    fake_registry = SimpleNamespace(
        document=SimpleNamespace(workspaces={"ws-quiet": entry}),
        load=lambda: None,
    )
    monkeypatch.setattr(
        "src.workspace.updates.catalog.open_registry",
        lambda *args, **kwargs: fake_registry,
    )

    payload = build_updates_workspaces_payload(coara_home=home, workspace_dir=tmp_path)
    assert payload[0]["unread"] == 0
    assert payload[0]["latest"] is None

"""EventSourceManager registry lookup uses workspace name, not id keys."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.event_sources.manager import EventSourceManager
from src.workspace.registry import WorkspaceRegistry
from src.workspace.types import WorkspaceEntry, WorkspaceKind, WorkspaceStatus


class _FakeVfs:
    pass


@pytest.mark.asyncio
async def test_start_sources_resolves_workspace_by_name(tmp_path: Path) -> None:
    home = tmp_path / "coara"
    home.mkdir()
    (home / "registry").mkdir()
    (home / "users" / "default" / "matters" / ".state").mkdir(parents=True)
    (home / "users" / "default" / "inbox").mkdir(parents=True)

    ws_dir = tmp_path / "xuan_android_local"
    (ws_dir / ".coara" / "matters" / "definitions").mkdir(parents=True)
    entry = WorkspaceEntry(
        name="暄",
        id="xuan_android_local-deadbeef01",
        path=str(ws_dir),
        kind=WorkspaceKind.MANAGED,
        status=WorkspaceStatus.ACTIVE,
    )
    registry = WorkspaceRegistry(home)
    registry.document.workspaces = {entry.id: entry}
    registry.document.default_workspace = entry.id
    registry.save()

    defn = {
        "id": "xuan-feedback-webhook",
        "enabled": True,
        "kind": "webhook",
        "workspace": "暄",
        "webhook_secret": "test-secret",
    }
    yaml_path = ws_dir / ".coara" / "matters" / "definitions" / "xuan-feedback-webhook.yaml"
    yaml_path.write_text(yaml.safe_dump(defn, allow_unicode=True), encoding="utf-8")

    manager = EventSourceManager(
        SimpleNamespace(coara_home=home, registry=registry, vfs=_FakeVfs()),
        coara_id="root-test",
    )
    # Avoid binding a real port in unit tests.
    manager._webhook_server.port = 0
    await manager.start()
    try:
        assert "xuan-feedback-webhook" in manager._webhook_server._sources
        status = {row["id"]: row for row in manager.list_status()}
        assert status["xuan-feedback-webhook"]["running"] is True
    finally:
        await manager.stop()

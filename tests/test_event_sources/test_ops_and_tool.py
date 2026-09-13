"""Tests for event_sources.ops and event_source tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from src.event_sources.ops import (
    EventSourceOpsError,
    delete_definition,
    list_definitions,
    read_definition,
    write_definition,
)
from src.tools.builtin.scheduling.event_source import EventSourceTool
from src.workspace.registry import WorkspaceRegistry
from src.workspace.types import WorkspaceEntry


def _wm(tmp_path: Path, *, name: str = "shop") -> SimpleNamespace:
    home = tmp_path / "home"
    home.mkdir()
    reg = WorkspaceRegistry(home)
    entry = WorkspaceEntry(
        id="ws-1",
        name=name,
        path=str(tmp_path / "shop"),
    )
    (tmp_path / "shop").mkdir(exist_ok=True)
    reg.document.workspaces[entry.id] = entry
    reg.save()
    return SimpleNamespace(
        coara_home=home,
        registry=reg,
    )


def test_write_and_list_definition(tmp_path: Path) -> None:
    wm = _wm(tmp_path)
    defn = write_definition(
        wm.coara_home,
        {
            "id": "inbox-watch",
            "kind": "file_watch",
            "workspace": "shop",
            "watch_path": "feedback/inbox",
        },
        overwrite=False,
        registry=wm.registry,
    )
    assert defn.id == "inbox-watch"
    rows = list_definitions(wm.coara_home, registry=wm.registry)
    assert len(rows) == 1
    loaded = read_definition(wm.coara_home, "inbox-watch", registry=wm.registry)
    assert loaded.watch_path == "feedback/inbox"
    # 空间自治布局：落 <ws>/.coara/matters/definitions/
    path = tmp_path / "shop" / ".coara" / "matters" / "definitions" / "inbox-watch.yaml"
    assert path.is_file()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["kind"] == "file_watch"


def test_write_definition_falls_back_to_legacy_without_registry(tmp_path: Path) -> None:
    """无 registry（兼容路径）时仍写旧单一目录。"""
    wm = _wm(tmp_path)
    write_definition(
        wm.coara_home,
        {"id": "legacy-hook", "kind": "webhook", "workspace": "shop"},
        overwrite=False,
    )
    path = wm.coara_home / "users" / "default" / "matters" / "definitions" / "legacy-hook.yaml"
    assert path.is_file()


def test_reject_bad_id(tmp_path: Path) -> None:
    wm = _wm(tmp_path)
    with pytest.raises(EventSourceOpsError):
        write_definition(
            wm.coara_home,
            {"id": "../evil", "kind": "webhook", "workspace": "shop"},
            overwrite=False,
        )


def test_delete_definition(tmp_path: Path) -> None:
    wm = _wm(tmp_path)
    write_definition(
        wm.coara_home,
        {"id": "x", "kind": "webhook", "workspace": "shop"},
        overwrite=False,
        registry=wm.registry,
    )
    delete_definition(wm.coara_home, "x", registry=wm.registry)
    assert list_definitions(wm.coara_home, registry=wm.registry) == []


@pytest.mark.asyncio
async def test_event_source_tool_add(tmp_path: Path) -> None:
    wm = _wm(tmp_path)
    root = SimpleNamespace(
        workspace_manager=wm,
        event_source_manager=SimpleNamespace(reload=AsyncMock()),
        foreground_active_name=lambda: "shop",
    )
    tool = EventSourceTool(parent_coara=root)
    inv = tool.create_invocation(
        {
            "action": "add",
            "id": "app-hook",
            "kind": "webhook",
            "workspace": "shop",
            "webhook_secret": "s",
        }
    )
    result = await inv.execute()
    assert not result.is_error
    assert "已创建" in result.content
    root.event_source_manager.reload.assert_awaited()
    loaded = read_definition(wm.coara_home, "app-hook", registry=wm.registry)
    assert loaded.kind.value == "webhook"
    # 空间自治布局落点
    assert (tmp_path / "shop" / ".coara" / "matters" / "definitions" / "app-hook.yaml").is_file()

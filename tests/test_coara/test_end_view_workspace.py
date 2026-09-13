"""Per-end view_workspace：取消全局前台特权。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.coara.commands import workspace as ws_cmd
from src.coara.commands.registry import CommandArgs


def _entry(wid: str, name: str, path: Path, *, ends: tuple[str, ...] = ("cli", "web", "matrix")):
    e = SimpleNamespace(
        id=wid,
        name=name,
        path=path,
        mode=SimpleNamespace(value="local"),
        view="default",
        summary="",
    )
    e.end_allowed = lambda end, _ends=ends: end in _ends
    e.resolved_path = lambda: path
    return e


@pytest.mark.asyncio
async def test_set_view_workspace_isolates_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """matrix/web/cli 各切各的，互不覆盖。"""
    from src.coara.root import RootCoara

    root = object.__new__(RootCoara)
    root._sessions = {}
    root._sessions_lock = __import__("asyncio").Lock()
    root._foreground_session_id = "id-v8"
    root._cli_view_workspace_id = "id-v8"
    root._web_view_workspace_id = "id-v8"
    root._matrix_view_workspace_id = "id-v8"
    root._workspace_activity_at = {}
    root.workspace_dir = tmp_path / "v8"
    root.identity = SimpleNamespace(coara_id="root", name="root", workspace_dir=root.workspace_dir)
    root.event_bus = MagicMock()
    root.last_switch_session_renewed = False
    root.last_switch_last_active = None

    v8 = tmp_path / "v8"
    nx = tmp_path / "nx"
    v8.mkdir()
    nx.mkdir()
    e_v8 = _entry("id-v8", "v8", v8)
    e_nx = _entry("id-nx", "nx", nx)

    registry = MagicMock()
    registry.resolve_name_or_id = lambda ref: {"v8": e_v8, "nx": e_nx, "id-v8": e_v8, "id-nx": e_nx}.get(ref)
    manager = MagicMock()
    manager.registry = registry
    manager.switch = MagicMock(return_value=True)
    manager.coara_home = tmp_path / "home"
    manager.active_path = v8
    root.workspace_manager = manager

    sessions: dict[str, Any] = {}

    async def ensure(entry):
        if entry.id not in sessions:
            coara = SimpleNamespace(
                workspace_dir=entry.resolved_path(),
                session_id=f"sess-{entry.id}",
                message_history=[],
                provider_name="deepseek",
                model_name="deepseek-chat",
                is_plan_mode=lambda: False,
            )
            sessions[entry.id] = SimpleNamespace(coara=coara)
            root._sessions[entry.id] = sessions[entry.id]
        return sessions[entry.id]

    root.ensure_workspace_session = ensure  # type: ignore[method-assign]
    root.sync_workspace_manager_to_foreground = lambda: True  # type: ignore[method-assign]
    root.start_new_session = AsyncMock()

    monkeypatch.setattr(
        "src.coara.workspace_runtime.publish_active_runtime",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "src.coara.workspace_runtime.load_active_runtime",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.coara.workspace_state.is_session_stale",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "src.coara.workspace_state.workspace_session_has_conversation",
        lambda *_a, **_k: True,
    )

    assert await root.set_view_workspace("matrix", "nx")
    assert root.view_workspace_id("matrix") == "id-nx"
    assert root.view_workspace_id("web") == "id-v8"
    assert root.view_workspace_id("cli") == "id-v8"

    assert await root.set_view_workspace("web", "nx")
    assert root.view_workspace_id("web") == "id-nx"
    assert root.view_workspace_id("cli") == "id-v8"
    assert root.view_workspace_id("matrix") == "id-nx"

    assert await root.set_view_workspace("cli", "nx", republish_runtime=False)
    assert root.view_workspace_id("cli") == "id-nx"
    assert root._foreground_session_id == "id-nx"

    # publish(TraceEvent(...)) — 取最后一个 workspace_switched payload
    ws_payloads = []
    for call in root.event_bus.publish.call_args_list:
        ev = call.args[0] if call.args else None
        if ev is not None and getattr(ev, "event_type", "") == "workspace_switched":
            ws_payloads.append(getattr(ev, "payload", {}) or {})
    assert ws_payloads, "cli view switch must emit workspace_switched"
    last = ws_payloads[-1]
    assert last.get("previous_workspace_id") == "id-v8"
    assert last.get("workspace_id") == "id-nx"
    assert last.get("provider_name") == "deepseek"
    assert last.get("model_name") == "deepseek-chat"
    assert last.get("workspace_name") == "nx"
    assert last.get("end") == "cli"
    # matrix/web 只发 view_changed，不发 workspace_switched（手机不得跟 CLI）
    view_ends = []
    for call in root.event_bus.publish.call_args_list:
        ev = call.args[0] if call.args else None
        if ev is not None and getattr(ev, "event_type", "") == "view_changed":
            view_ends.append((getattr(ev, "payload", {}) or {}).get("end"))
    assert "matrix" in view_ends and "web" in view_ends
    assert all(p.get("end") == "cli" for p in ws_payloads)


@pytest.mark.asyncio
async def test_ws_switch_command_routes_by_origin(tmp_path: Path) -> None:
    root = MagicMock()
    root.normalize_view_end = lambda s: {
        "matrix": "matrix",
        "web": "web",
        "cli": "cli",
        "cli-attached": "cli",
        "": "cli",
    }.get(s, "cli")
    root.set_view_workspace = AsyncMock(return_value=True)
    view = SimpleNamespace(workspace_dir=tmp_path / "nx", session_id="s1")
    root.resolve_view_coara = MagicMock(return_value=view)
    entry = _entry("id-nx", "nx", tmp_path / "nx")
    root.workspace_manager = MagicMock()
    root.workspace_manager.registry.resolve_name_or_id = MagicMock(return_value=entry)
    root.workspace_manager.set_persistent_default = MagicMock(return_value=False)
    root.view_workspace_id = MagicMock(return_value="id-nx")
    root.last_switch_session_renewed = False
    root.last_switch_last_active = None

    args = CommandArgs(name="ws", parts=["ws", "switch", "nx"], sub="switch", value="nx", raw="/ws switch nx")
    args.origin_source = "matrix"
    result = await ws_cmd._handle_ws_switch(root, args)
    assert result.action == "switch_workspace"
    root.set_view_workspace.assert_awaited_with("matrix", "nx")
    assert result.data["view"] == "matrix"

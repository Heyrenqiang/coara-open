"""Tests for the CLI `coara ws remove` active-process guard (F2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.cli import workspace_cmds
from src.coara.workspace_runtime import publish_active_runtime


@pytest.fixture
def registry_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "src.workspace.registry.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    coara_home = tmp_path / "home"

    async def _fake_home() -> Path:
        return coara_home

    monkeypatch.setattr(workspace_cmds, "_load_config_coara_home", _fake_home)
    monkeypatch.setattr(workspace_cmds, "_cli_registry_cache", None)

    workspace = tmp_path / "cli-ws"
    workspace.mkdir()
    other = tmp_path / "other-ws"
    other.mkdir()

    registry = workspace_cmds._open_cli_registry(tmp_path)
    registry.ensure_workspace(workspace, name="cli-ws")
    registry.ensure_workspace(other, name="other-ws")
    registry.save()
    return tmp_path, coara_home, workspace, other


def _runtime_for(coara_home: Path, workspace: Path):
    return publish_active_runtime(
        coara_home=coara_home,
        workspace_path=workspace,
        workspace_name=workspace.name,
        session_id="s",
        coara_id="c",
        coara_name="root",
    )


def test_remove_refuses_workspace_bound_to_live_process(registry_env, capsys) -> None:
    workspace_arg, coara_home, workspace, _ = registry_env
    _runtime_for(coara_home, workspace)  # pid = current (live) process

    workspace_cmds.remove_workspace(workspace_arg, "cli-ws")

    out = capsys.readouterr().out
    assert "拒绝移除" in out
    registry = workspace_cmds._open_cli_registry(workspace_arg)
    assert registry.resolve_name_or_id("cli-ws") is not None


def test_remove_allowed_when_runtime_points_elsewhere(registry_env, capsys) -> None:
    workspace_arg, coara_home, workspace, other = registry_env
    _runtime_for(coara_home, other)

    workspace_cmds.remove_workspace(workspace_arg, "cli-ws")

    out = capsys.readouterr().out
    assert "已取消登记" in out
    registry = workspace_cmds._open_cli_registry(workspace_arg)
    assert registry.resolve_name_or_id("cli-ws") is None
    assert workspace.is_dir()


def test_remove_allowed_when_no_live_runtime(registry_env, capsys) -> None:
    workspace_arg, coara_home, workspace, _ = registry_env
    runtime = _runtime_for(coara_home, workspace)
    # Simulate a dead publisher: pid almost certainly not alive.
    from dataclasses import asdict

    from src.coara.workspace_runtime import runtime_file
    from src.core.json_store import write_json_atomic

    runtime.pid = 999_999_999
    write_json_atomic(runtime_file(coara_home), asdict(runtime))

    workspace_cmds.remove_workspace(workspace_arg, "cli-ws")

    out = capsys.readouterr().out
    assert "已取消登记" in out
    registry = workspace_cmds._open_cli_registry(workspace_arg)
    assert registry.resolve_name_or_id("cli-ws") is None

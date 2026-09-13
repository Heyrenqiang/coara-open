"""Busy workspace switch leaves the origin turn running (no interrupt)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.coara.root import RootCoara
from src.llm.provider import LLMProvider
from src.llm.registry import provider_registry
from src.workspace.manager import WorkspaceManager


class _NoopProvider(LLMProvider):
    async def complete(self, *args, **kwargs):
        from src.llm.provider import LLMResponse

        return LLMResponse(content="")

    async def stream_complete(self, *args, **kwargs):
        from src.llm.provider import StreamChunk

        yield StreamChunk()

    def get_context_window(self, model: str | None = None) -> int:
        return 32_000

    def abort(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.fixture
async def root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    provider_registry.register(
        "switch-lock-test", _NoopProvider(name="switch-lock-test", api_key="x", default_model="m")
    )

    root = RootCoara(workspace_dir=workspace_a, provider_name="switch-lock-test")
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    root.workspace_manager.registry.ensure_workspace(workspace_b, name="b")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id

    original_cwd = Path.cwd()
    os.chdir(workspace_a)
    try:
        yield root
    finally:
        os.chdir(original_cwd)


@pytest.mark.asyncio
async def test_non_tool_switch_does_not_interrupt_busy_foreground(root: RootCoara, monkeypatch) -> None:
    """Human/API switch while A is busy: leave A's turn running; focus moves to B."""
    fg = root.foreground_coara
    calls: list[str] = []
    monkeypatch.setattr(fg, "has_active_turn", lambda: True)

    def _spy_interrupt(reason, *, interrupt_source=None):
        calls.append(reason)
        return True

    monkeypatch.setattr(fg, "interrupt_current_turn", _spy_interrupt)

    success = await root.switch_workspace("b")
    assert success
    assert calls == []
    assert root.workspace_manager.active_entry is not None
    assert root.workspace_manager.active_entry.name == "b"


@pytest.mark.asyncio
async def test_switch_without_active_turn_does_not_interrupt(root: RootCoara, monkeypatch) -> None:
    fg = root.foreground_coara
    calls: list[str] = []

    def _spy_interrupt(reason, *, interrupt_source=None):
        calls.append(reason)
        return True

    monkeypatch.setattr(fg, "interrupt_current_turn", _spy_interrupt)

    success = await root.switch_workspace("b")
    assert success
    assert calls == []

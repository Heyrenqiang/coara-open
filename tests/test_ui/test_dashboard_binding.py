"""Tests for dashboard workspace/token binding consistency."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.runtime.tool_output_store import ToolOutputStore
from src.ui.dashboard_binding import resolve_configured_coara_home, resolve_dashboard_binding
from src.ui.dashboard_handlers import DashboardRestHandlers
from src.ui.dashboard_tokens import load_or_create_dashboard_token


def _install_active_runtime(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (coara_home, requested_workspace, live_workspace) with active.json wired."""
    home = tmp_path / "coara_home"
    home.mkdir()
    requested = tmp_path / "requested"
    requested.mkdir()
    live = tmp_path / "live-workspace"
    live.mkdir()

    runtime_dir = home / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "active.json").write_text(
        json.dumps(
            {
                "workspace_id": "live-ws",
                "workspace_path": str(live),
                "alias": "live",
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    return home, requested, live


def test_binding_uses_active_runtime(tmp_path: Path) -> None:
    home, requested, live = _install_active_runtime(tmp_path)

    binding = resolve_dashboard_binding(requested, coara_home=home)
    assert binding.workspace.resolve() == live.resolve()
    assert binding.source == "active_runtime"

    # Token is shared across workspaces under the same coara Home.
    expected_token = load_or_create_dashboard_token(binding.workspace, binding.coara_home)
    assert expected_token == load_or_create_dashboard_token(requested, home)


def test_binding_prefer_active_runtime_flag(tmp_path: Path) -> None:
    home, requested, live = _install_active_runtime(tmp_path)

    live_binding = resolve_dashboard_binding(requested, coara_home=home, prefer_active_runtime=True)
    workspace_binding = resolve_dashboard_binding(requested, coara_home=home, prefer_active_runtime=False)
    live_token = load_or_create_dashboard_token(live_binding.workspace, live_binding.coara_home)
    workspace_token = load_or_create_dashboard_token(workspace_binding.workspace, workspace_binding.coara_home)

    assert live_binding.workspace.resolve() == live.resolve()
    assert workspace_binding.workspace.resolve() == requested.resolve()
    # Both resolve to the same home-level token.
    assert live_token == load_or_create_dashboard_token(live, home)
    assert workspace_token == load_or_create_dashboard_token(requested, home)
    assert live_token == workspace_token


def test_binding_respects_workspace_flag_when_active_runtime_disabled(tmp_path: Path) -> None:
    home, requested, live = _install_active_runtime(tmp_path)

    binding = resolve_dashboard_binding(requested, coara_home=home, prefer_active_runtime=False)
    assert binding.workspace.resolve() == requested.resolve()
    assert binding.source == "workspace_flag"

    expected_token = load_or_create_dashboard_token(binding.workspace, binding.coara_home)
    assert expected_token == load_or_create_dashboard_token(requested, home)
    assert live.resolve() != requested.resolve()


def test_resolve_configured_coara_home_prefers_loaded_config_over_stale_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.config import CoaraConfig, config_manager

    real_home = tmp_path / "real_coara"
    stale_home = tmp_path / "stale_coara"
    real_home.mkdir()
    stale_home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(stale_home))

    config_manager._config = CoaraConfig(coara_home=real_home)
    try:
        assert resolve_configured_coara_home().resolve() == real_home.resolve()
        assert resolve_configured_coara_home(stale_home).resolve() == stale_home.resolve()
    finally:
        config_manager._config = None


# --- tool outputs api ---


def _make_handlers_app(handlers: DashboardRestHandlers) -> web.Application:
    """Build an aiohttp app with only the dashboard REST routes registered."""
    app = web.Application()
    handlers.register_routes(app.router)
    return app


@pytest.mark.asyncio
async def test_api_tool_outputs_list_and_read(tmp_path: Path, isolated_coara_home: Path) -> None:
    store = ToolOutputStore(workspace_dir=tmp_path, session_id="sess-ui", coara_home=isolated_coara_home)
    record = store.write(
        tool_name="grep",
        tool_call_id="call-ui",
        content="alpha\nbeta\ngamma",
        arguments={"pattern": "a"},
    )

    handlers = DashboardRestHandlers(tmp_path, coara_home=isolated_coara_home)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        listed = await (await client.get(f"/api/tool-outputs?token={token}")).json()
        assert listed["count"] == 1
        assert listed["outputs"][0]["ref"] == record.ref

        detail = await (
            await client.get(f"/api/tool-output/{record.ref}?token={token}&session_id=sess-ui&offset=1&limit=2")
        ).json()
        assert detail["tool_name"] == "grep"
        assert "alpha" in detail["content"]
        assert detail["truncated"] is True
        assert detail["total_lines"] == 3


@pytest.mark.asyncio
async def test_api_tool_output_invalid_ref_rejected(tmp_path: Path) -> None:
    handlers = DashboardRestHandlers(tmp_path)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        resp = await client.get(f"/api/tool-output/not-valid?token={token}")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_api_requires_token(tmp_path: Path) -> None:
    """REST endpoints reject requests without a valid dashboard token."""
    handlers = DashboardRestHandlers(tmp_path)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        missing = await client.get("/api/tool-outputs")
        assert missing.status == 401

        wrong = await client.get("/api/tool-outputs?token=wrong-token")
        assert wrong.status == 401

        ok = await client.get(f"/api/tool-outputs?token={token}")
        assert ok.status == 200

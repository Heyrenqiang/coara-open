"""Security middleware + providers masking (audit 2026-08-14 P1#5/#6/#7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.core.config import config_manager, reset_config_manager_for_tests
from src.ui.control_plane import build_providers_envelope
from src.ui.dashboard_handlers import DashboardRestHandlers
from src.ui.web_server import WebServer


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config_manager_for_tests()
    yield
    reset_config_manager_for_tests()


def _make_app(tmp_path: Path) -> web.Application:
    """Minimal app wired like WebServer's: middleware + /attachments static."""
    server = WebServer.__new__(WebServer)
    server.workspace_dir = tmp_path
    server.coara_home = None
    app = web.Application(middlewares=[server._version_header_middleware])
    attachments = tmp_path / ".coara" / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    (attachments / "secret.png").write_bytes(b"png")
    app.router.add_static("/attachments", attachments, show_index=False)

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text="<html><head></head><body>ok</body></html>", content_type="text/html")

    app.router.add_get("/", index)
    return app


def _token(tmp_path: Path) -> str:
    from src.ui.dashboard_tokens import load_or_create_dashboard_token

    return load_or_create_dashboard_token(tmp_path, None)


@pytest.mark.asyncio
async def test_attachments_requires_token(tmp_path: Path) -> None:
    async with TestClient(TestServer(_make_app(tmp_path))) as client:
        resp = await client.get("/attachments/secret.png")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_attachments_serves_with_valid_token(tmp_path: Path) -> None:
    token = _token(tmp_path)
    async with TestClient(TestServer(_make_app(tmp_path))) as client:
        resp = await client.get(f"/attachments/secret.png?token={token}")
        assert resp.status == 200
        assert await resp.read() == b"png"


@pytest.mark.asyncio
async def test_all_responses_carry_referrer_policy(tmp_path: Path) -> None:
    token = _token(tmp_path)
    async with TestClient(TestServer(_make_app(tmp_path))) as client:
        for url in ("/", f"/attachments/secret.png?token={token}"):
            resp = await client.get(url)
            assert resp.headers.get("Referrer-Policy") == "no-referrer", url


def test_index_referrer_meta_injected() -> None:
    injected = WebServer._inject_referrer_policy_meta("<html><head><title>t</title></head></html>")
    assert '<meta name="referrer" content="no-referrer" />' in injected
    # Already present → untouched
    again = WebServer._inject_referrer_policy_meta(injected)
    assert again.count("no-referrer") == 1


def test_build_providers_envelope_masks_inline_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.config import config_manager

    monkeypatch.setattr(
        config_manager,
        "get_raw_config",
        lambda: {
            "providers": {
                "zhipu": {"base_url": "https://x", "api_key": "sk-live-SECRET"},
                "deepseek": {"base_url": "https://y", "api_key_env": "DEEPSEEK_API_KEY"},
            }
        },
    )
    envelope = build_providers_envelope()
    assert envelope["providers"]["zhipu"]["api_key"] == "***"
    assert envelope["providers"]["deepseek"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert envelope["providers"]["zhipu"]["base_url"] == "https://x"


@pytest.mark.asyncio
async def test_providers_save_restores_masked_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.coara_home import system_dir_for_home

    home = tmp_path / "home"
    monkeypatch.setenv("COARA_HOME", str(home))
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [str(home)])
    system_dir = system_dir_for_home(home)
    system_dir.mkdir(parents=True)
    (system_dir / "providers.yaml").write_text(
        "providers:\n  zhipu:\n    base_url: https://x\n    api_key: sk-real\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    await config_manager.load()

    class _Req:
        def __init__(self, payload, token):
            self._payload = payload
            self.query = {"token": token}

        async def json(self):
            return self._payload

    handlers = DashboardRestHandlers.__new__(DashboardRestHandlers)
    handlers.workspace_dir = workspace
    handlers.coara_home = home.resolve()
    handlers.auth_token = "unused"  # overwritten by _check_token
    from src.ui.dashboard_tokens import load_or_create_dashboard_token

    token = load_or_create_dashboard_token(workspace, handlers.coara_home)
    # Save echoes back the masked GET payload verbatim.
    masked = {
        "providers": {"zhipu": {"base_url": "https://x", "api_key": "***"}},
    }
    response = await handlers.handle_api_v1_providers_save(_Req(masked, token))
    assert response.status == 200

    import yaml

    saved = yaml.safe_load((system_dir / "providers.yaml").read_text(encoding="utf-8"))
    assert saved["providers"]["zhipu"]["api_key"] == "sk-real"

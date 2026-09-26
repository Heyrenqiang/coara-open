"""开机自启与用户规则两条配置 API 的集成测试（dashboard_handlers）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from src.core.coara_home import system_dir_for_home
from src.core.config import reset_config_manager_for_tests
from src.ui.dashboard_handlers import DashboardRestHandlers


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config_manager_for_tests()
    yield
    reset_config_manager_for_tests()


@pytest.fixture
def coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "coara_home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [str(home)])
    return home


def _make_handlers(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DashboardRestHandlers:
    handlers = DashboardRestHandlers.__new__(DashboardRestHandlers)
    handlers.coara_home = coara_home
    handlers.workspace_dir = tmp_path / "workspace"
    handlers.workspace_dir.mkdir()
    handlers.auth_token = "test-token"
    # 校验逻辑本身不在本组用例射程内（dashboard_auth 另有覆盖），直通放行
    monkeypatch.setattr("src.ui.handlers.base.check_dashboard_token", lambda request, **kw: "test-token")
    return handlers


def _authed_request(payload: dict | None = None) -> Any:
    req = SimpleNamespace(
        headers={"X-Coara-Token": "test-token"},
        query={},
    )
    if payload is not None:
        async def _json():
            return payload
        req.json = _json
    return req


@pytest.mark.asyncio
async def test_autostart_get(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)
    monkeypatch.setattr("src.cli.autostart.is_enabled", lambda: True)
    monkeypatch.setattr("src.cli.autostart._launcher_command", lambda: '"coara.exe" tray')
    resp = await handlers.handle_api_v1_autostart(_authed_request())
    assert resp.status == 200
    import json

    body = json.loads(resp.body)
    assert body["enabled"] is True
    assert "tray" in body["command"]


@pytest.mark.asyncio
async def test_autostart_set_requires_bool(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)
    with pytest.raises(web.HTTPBadRequest):
        await handlers.handle_api_v1_autostart_set(_authed_request({"enabled": "false"}))
    with pytest.raises(web.HTTPBadRequest):
        await handlers.handle_api_v1_autostart_set(_authed_request({"enabled": 1}))


@pytest.mark.asyncio
async def test_autostart_set_enable_disable(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("src.cli.autostart.enable", lambda: calls.append("on") or "ok")
    monkeypatch.setattr("src.cli.autostart.disable", lambda: calls.append("off") or "ok")
    monkeypatch.setattr("src.cli.autostart.is_enabled", lambda: calls[-1] == "on" if calls else False)

    resp = await handlers.handle_api_v1_autostart_set(_authed_request({"enabled": True}))
    assert resp.status == 200 and calls == ["on"]
    resp = await handlers.handle_api_v1_autostart_set(_authed_request({"enabled": False}))
    assert calls == ["on", "off"]


@pytest.mark.asyncio
async def test_autostart_set_os_write_failure(
    coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)

    def _boom():
        raise OSError("registry locked")

    monkeypatch.setattr("src.cli.autostart.enable", _boom)
    with pytest.raises(web.HTTPInternalServerError):
        await handlers.handle_api_v1_autostart_set(_authed_request({"enabled": True}))


@pytest.mark.asyncio
async def test_user_rules_read_missing_returns_empty(
    coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)
    resp = await handlers.handle_api_v1_user_rules(_authed_request())
    assert resp.status == 200
    import json

    assert json.loads(resp.body)["content"] == ""


@pytest.mark.asyncio
async def test_user_rules_save_and_read_back(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)
    resp = await handlers.handle_api_v1_user_rules_save(_authed_request({"content": "回复一律简体中文"}))
    assert resp.status == 200
    path = system_dir_for_home(coara_home) / "user_rules.md"
    assert path.read_text(encoding="utf-8") == "回复一律简体中文"
    resp = await handlers.handle_api_v1_user_rules(_authed_request())
    import json

    assert json.loads(resp.body)["content"] == "回复一律简体中文"


@pytest.mark.asyncio
async def test_user_rules_save_rejects_non_string(
    coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)
    with pytest.raises(web.HTTPBadRequest):
        await handlers.handle_api_v1_user_rules_save(_authed_request({"content": 123}))


@pytest.mark.asyncio
async def test_user_rules_save_rejects_over_limit(
    coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _make_handlers(coara_home, tmp_path, monkeypatch)
    big = "x" * (handlers._USER_RULES_MAX_CHARS + 1)
    with pytest.raises(web.HTTPBadRequest):
        await handlers.handle_api_v1_user_rules_save(_authed_request({"content": big}))

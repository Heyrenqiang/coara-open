"""可选能力 web 路由注册回归。

闭源发行版装了实现包（src/account、src/telemetry），这四条账户接口与一条遥测
中继接口必须照旧挂上；开源发行版删掉实现包后门面返回 False，路由不存在。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from src.ext import register_account_web, register_telemetry_web


def _server(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        coara_home=str(tmp_path),
        root=SimpleNamespace(telemetry_service=None),
        _check_token=lambda request: None,
    )


def _paths(router: web.UrlDispatcher) -> set[str]:
    return {route.resource.canonical for route in router.routes()}


def test_bundled_account_routes_are_registered(tmp_path: Path) -> None:
    router = web.UrlDispatcher()
    assert register_account_web(router, _server(tmp_path)) is True
    paths = _paths(router)
    assert "/api/v1/account/status" in paths
    assert "/api/v1/account/request-code" in paths
    assert "/api/v1/account/verify" in paths
    assert "/api/v1/account/logout" in paths


def test_bundled_telemetry_route_is_registered(tmp_path: Path) -> None:
    router = web.UrlDispatcher()
    assert register_telemetry_web(router, _server(tmp_path)) is True
    assert "/api/v1/telemetry/event" in _paths(router)

"""Tests for GET /api/v1/kernel/busy (pending-update turn guard)."""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.ui.web_server import WebServer
from tests.helpers import make_test_coara


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_server(workspace: Path, root: object | None = None) -> WebServer:
    server = WebServer(make_test_coara(workspace), workspace_dir=workspace, port=_free_port())
    if root is not None:
        server.root = root
    return server


def _make_app(server: WebServer) -> web.Application:
    app = web.Application()
    app.router.add_get("/api/v1/kernel/busy", server._handle_kernel_busy)
    return app


@pytest.mark.asyncio
async def test_busy_endpoint_requires_token(tmp_path: Path) -> None:
    server = _make_server(tmp_path, root=SimpleNamespace(_sessions={}))
    async with TestClient(TestServer(_make_app(server))) as client:
        resp = await client.get("/api/v1/kernel/busy")
        assert resp.status == 401
        resp = await client.get("/api/v1/kernel/busy", params={"token": "wrong"})
        assert resp.status == 401


@pytest.mark.asyncio
async def test_idle_kernel_reports_not_busy(tmp_path: Path) -> None:
    idle = SimpleNamespace(has_active_turn=lambda: False)
    root = SimpleNamespace(_sessions={"ws1": SimpleNamespace(coara=idle)})
    server = _make_server(tmp_path, root=root)
    async with TestClient(TestServer(_make_app(server))) as client:
        resp = await client.get("/api/v1/kernel/busy", params={"token": server.auth_token})
        assert resp.status == 200
        data = await resp.json()
        assert data == {"busy": False, "active_turns": 0, "background_tasks": 0}


@pytest.mark.asyncio
async def test_active_turn_marks_busy(tmp_path: Path) -> None:
    idle = SimpleNamespace(has_active_turn=lambda: False)
    busy_coara = SimpleNamespace(has_active_turn=lambda: True)
    root = SimpleNamespace(_sessions={"ws1": SimpleNamespace(coara=idle), "ws2": SimpleNamespace(coara=busy_coara)})
    server = _make_server(tmp_path, root=root)
    async with TestClient(TestServer(_make_app(server))) as client:
        resp = await client.get("/api/v1/kernel/busy", params={"token": server.auth_token})
        assert resp.status == 200
        data = await resp.json()
        assert data["busy"] is True
        assert data["active_turns"] == 1


@pytest.mark.asyncio
async def test_background_subagent_marks_busy(tmp_path: Path) -> None:
    from src.tools.builtin.delegate import delegate as delegate_mod

    fake = SimpleNamespace(has_active_turn=lambda: False)
    root = SimpleNamespace(_sessions={"ws1": SimpleNamespace(coara=fake)})
    server = _make_server(tmp_path, root=root)
    delegate_mod._RUNNING_SUBAGENTS["sub-1"] = SimpleNamespace()
    try:
        async with TestClient(TestServer(_make_app(server))) as client:
            resp = await client.get("/api/v1/kernel/busy", params={"token": server.auth_token})
            assert resp.status == 200
            data = await resp.json()
            assert data["busy"] is True
            assert data["background_tasks"] >= 1
    finally:
        delegate_mod._RUNNING_SUBAGENTS.pop("sub-1", None)


@pytest.mark.asyncio
async def test_no_sessions_root_falls_back_to_root_turn_state(tmp_path: Path) -> None:
    root = SimpleNamespace(_sessions={}, has_active_turn=lambda: True)
    server = _make_server(tmp_path, root=root)
    async with TestClient(TestServer(_make_app(server))) as client:
        resp = await client.get("/api/v1/kernel/busy", params={"token": server.auth_token})
        assert resp.status == 200
        data = await resp.json()
        assert data["busy"] is True
        assert data["active_turns"] == 1

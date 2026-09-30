"""Tests for dashboard control-plane helpers."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.ui.control_plane import discover_skills_payload
from src.ui.dashboard_handlers import DashboardRestHandlers
from src.ui.web_server import WebServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_handlers_app(handlers: DashboardRestHandlers) -> web.Application:
    """Build an aiohttp app with only the dashboard REST routes registered."""
    app = web.Application()
    handlers.register_routes(app.router)
    return app


@pytest.mark.asyncio
async def test_discover_skills_payload_finds_builtin_skills(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    skills = await discover_skills_payload(repo_root)
    names = {s["name"] for s in skills}
    assert "workflow" not in names
    assert {"event-source", "skill-creator", "tool-creator", "create-rule", "工作空间管理"} <= names
    assert "research" not in names
    assert "self_demo" not in names


@pytest.mark.asyncio
async def test_api_v1_meta_and_skills(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    handlers = DashboardRestHandlers(repo_root)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        meta = await (await client.get(f"/api/v1/meta?token={token}")).json()
        assert meta["capabilities"]
        assert meta["dashboard_build_id"]
        # 不依赖仓库目录名：断言返回的就是构造 handlers 时给的那个仓库根
        assert Path(str(meta["workspace"])).resolve() == repo_root.resolve()

        skills = await (await client.get(f"/api/v1/skills?token={token}")).json()
        assert skills["pool_count"] >= 5
        assert isinstance(skills["skills"], list)


@pytest.mark.asyncio
async def test_skill_deferred_toggle_roundtrip() -> None:
    """挂起开关：payload 带 deferred 标记，POST 切换后配置与读面同步。"""
    from src.core.config import config_manager

    repo_root = Path(__file__).resolve().parents[2]
    await config_manager.load()

    # 初始：未配置名单，全部常驻
    payload = await discover_skills_payload(repo_root)
    by_name = {s["name"]: s for s in payload}
    assert by_name["event-source"]["deferred"] is False

    handlers = DashboardRestHandlers(repo_root)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        resp = await client.post(
            f"/api/v1/skills/deferred?token={token}",
            json={"name": "event-source", "deferred": True},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "event-source" in data["deferred"]

        listed = await (await client.get(f"/api/v1/skills?token={token}")).json()
        by_name = {s["name"]: s for s in listed["skills"]}
        assert by_name["event-source"]["deferred"] is True
        assert "event-source" in listed["deferred"]

        # 配置已落盘
        assert "event-source" in (config_manager.get_raw_config().get("skills", {}).get("deferred") or [])

        # 取消挂起
        resp = await client.post(
            f"/api/v1/skills/deferred?token={token}",
            json={"name": "event-source", "deferred": False},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "event-source" not in data["deferred"]

        # 不存在的技能 404
        resp = await client.post(
            f"/api/v1/skills/deferred?token={token}",
            json={"name": "no-such-skill", "deferred": True},
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_web_trace_events_includes_session_auto_new(tmp_path: Path) -> None:
    from src.coara.root import RootCoara
    from src.core.config import config_manager
    from src.core.events import TraceEvent
    from src.llm.registry import provider_registry
    from tests.helpers import FakeProvider

    await config_manager.load()
    provider_registry.register("fake-web-trace", FakeProvider([]))
    root = RootCoara(workspace_dir=tmp_path, provider_name="fake-web-trace")
    server = WebServer(root, workspace_dir=tmp_path, port=_free_port())
    await server.start()
    token = server.auth_token
    try:
        event = TraceEvent(
            coara_id=root.identity.coara_id,
            coara_name=root.identity.name,
            event_type="tool_call",
            message="tool",
            payload={
                "session_id": root.session_id,
                "tool_name": "read",
                "call_id": "call-test-1",
                "origin_scope": "main_loop",
            },
        )
        server.trace_store.append_event(event)

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.get(f"/api/trace/events?token={token}&kinds=tool&limit=20")
            data = await resp.json()
            event_types = {row["type"] for row in data.get("events", [])}
            assert "tool_call" in event_types
            assert any(row.get("tool") == "read" for row in data.get("events", []))
    finally:
        await server.stop()
        await root.shutdown()

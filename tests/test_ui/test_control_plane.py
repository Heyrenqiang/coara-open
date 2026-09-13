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
    assert "workflow" not in names  # workflow 已从技能退役（编排走挂起工具 orchestrator）
    # 内置技能现状（PPT/frontend-design/html/long-document 已裁撤）
    assert {"event-source", "research", "skill-creator", "工作空间管理"} <= names


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
        # 内置技能池已裁撤至 4 个（event-source/research/skill-creator/工作空间管理）
        assert skills["pool_count"] >= 4
        assert isinstance(skills["skills"], list)


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

"""Tests for WebUI settings-handlers CRUD endpoints.

覆盖：reminders / event-sources / workspaces 的 CRUD 与 token 守卫，
events 写后 reload，路径注入防护。
"""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.ui.settings_handlers import SettingsHandlers


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_app(handlers: SettingsHandlers) -> web.Application:
    app = web.Application()
    handlers.register_routes(app.router)
    return app


def _make_root_stub(tmp_path: Path) -> Any:
    """构造一个 RootCoara-形状的 stub，含 reminders / event-sources / workspaces。"""

    # ---------- reminder_service ----------
    reminders: dict[str, dict[str, Any]] = {}

    class _Reminder:
        async def add_one_time_reminder(self, message, *, minutes=0, hours=0, days=0):
            rid = f"once-{len(reminders)}"
            reminders[rid] = {"id": rid, "message": message, "enabled": True, "kind": "one_time"}
            return f"created {rid}"

        async def add_interval_reminder(self, message, *, minutes=0, hours=0, days=0):
            rid = f"int-{len(reminders)}"
            reminders[rid] = {"id": rid, "message": message, "enabled": True, "kind": "interval"}
            return f"created {rid}"

        async def add_cron_reminder(self, cron, message):
            rid = f"cron-{len(reminders)}"
            reminders[rid] = {
                "id": rid,
                "message": message,
                "cron": cron,
                "enabled": True,
                "kind": "cron",
            }
            return f"created {rid}"

        async def list_reminders(self):
            return list(reminders.values())

        async def remove_reminder(self, rid):
            if rid not in reminders:
                return f"missing {rid}"
            del reminders[rid]
            return f"deleted {rid}"

        async def set_reminder_enabled(self, rid, enabled):
            if rid not in reminders:
                raise ValueError(f"找不到 Reminder: {rid}")
            reminders[rid]["enabled"] = enabled
            return f"{'enabled' if enabled else 'disabled'} {rid}"

    # ---------- event_source_manager ----------
    es_loaded: dict[str, dict[str, Any]] = {}
    reload_calls: list[int] = []

    class _EsManager:
        def list_status(self):
            return [
                {"id": eid, "enabled": meta.get("enabled", True), "kind": meta.get("kind")}
                for eid, meta in es_loaded.items()
            ]

        async def reload(self):
            reload_calls.append(len(reload_calls))
            es_loaded.clear()
            # 空间自治布局：扫各工作空间 .coara/matters/definitions/
            for w in workspaces.values():
                dir_path = Path(w["path"]) / ".coara" / "matters" / "definitions"
                for p in sorted(dir_path.glob("*.yaml")):
                    with p.open(encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    es_loaded[data["id"]] = data

    # ---------- workspace_manager ----------
    workspaces: dict[str, dict[str, Any]] = {}

    class _Entry:
        """模拟 WorkspaceEntry：resolved_path 是方法（不进 vars 序列化）。"""

        def __init__(self, w: dict[str, Any]):
            self.id = w["id"]
            self.name = w["name"]
            self.path = w["path"]
            self.kind = w["kind"]
            self.summary = w["summary"]
            self.tags = w["tags"]
            self.status = w["status"]

        def resolved_path(self) -> Path:
            return Path(self.path)

    class _Registry:
        document = SimpleNamespace(default_workspace=None)

        @staticmethod
        def _entry_obj(w: dict[str, Any]) -> _Entry:
            return _Entry(w)

        def list_active(self):
            return [self._entry_obj(w) for w in workspaces.values()]

        def resolve_name_or_id(self, identifier: str):
            for w in workspaces.values():
                if w["id"] == identifier or w["name"] == identifier:
                    return self._entry_obj(w)
            return None

    class _WsManager:
        registry = _Registry()

        def add_workspace(self, path, *, name=None, summary=None):
            wid = f"ws-{len(workspaces)}"
            chosen = name or path.name
            entry = {
                "id": wid,
                "name": chosen,
                "path": str(path),
                "kind": "managed",
                "summary": summary or "",
                "tags": [],
                "status": "active",
            }
            workspaces[wid] = entry
            if _WsManager.registry.document.default_workspace is None:
                _WsManager.registry.document.default_workspace = wid
            return SimpleNamespace(**entry)

        def remove_workspace(self, wid):
            if wid not in workspaces:
                return False
            del workspaces[wid]
            if _WsManager.registry.document.default_workspace == wid:
                _WsManager.registry.document.default_workspace = next(iter(workspaces), None)
            return True

        def rename_workspace(self, wid, new_name):
            if wid not in workspaces:
                return None
            workspaces[wid]["name"] = new_name
            return SimpleNamespace(**workspaces[wid])

        def set_persistent_default(self, wid):
            if wid not in workspaces:
                return False
            _WsManager.registry.document.default_workspace = wid
            return True

    return SimpleNamespace(
        reminder_service=_Reminder(),
        event_source_manager=_EsManager(),
        workspace_manager=_WsManager(),
        _fixtures=SimpleNamespace(
            tmp_path=tmp_path,
            reminders=reminders,
            es_loaded=es_loaded,
            reload_calls=reload_calls,
            workspaces=workspaces,
        ),
    )


@pytest.fixture
def settings_env(tmp_path: Path):
    """构造 handlers + app，coara_home 落在 tmp_path。"""
    coara_home = tmp_path
    root = _make_root_stub(coara_home)
    handlers = SettingsHandlers(
        workspace_dir=tmp_path,
        coara_home=coara_home,
        root=root,
    )
    app = _make_app(handlers)
    return handlers, app, root


# ----------------------------------------------------------------------
# token 守卫
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_require_token(settings_env) -> None:
    _, app, _ = settings_env
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/v1/reminders")
        assert resp.status == 401


# ----------------------------------------------------------------------
# Reminders
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminders_crud(settings_env) -> None:
    handlers, app, _ = settings_env
    token = handlers.auth_token

    async with TestClient(TestServer(app)) as client:
        # one_time
        r1 = await client.post(
            f"/api/v1/reminders?token={token}",
            json={"kind": "one_time", "message": "喝水", "minutes": 30},
        )
        assert r1.status == 200, await r1.text()
        # interval
        r2 = await client.post(
            f"/api/v1/reminders?token={token}",
            json={"kind": "interval", "message": "起身", "minutes": 60},
        )
        assert r2.status == 200
        # cron
        r3 = await client.post(
            f"/api/v1/reminders?token={token}",
            json={"kind": "cron", "message": "晨检", "cron": "0 8 * * *"},
        )
        assert r3.status == 200

        rows = await (await client.get(f"/api/v1/reminders?token={token}")).json()
        assert len(rows["reminders"]) == 3

        rid = rows["reminders"][0]["id"]
        toggle = await client.post(
            f"/api/v1/reminders/{rid}/toggle?token={token}",
            json={"enabled": False},
        )
        assert toggle.status == 200

        delete = await client.delete(f"/api/v1/reminders/{rid}?token={token}")
        assert delete.status == 200
        rows = await (await client.get(f"/api/v1/reminders?token={token}")).json()
        assert len(rows["reminders"]) == 2


@pytest.mark.asyncio
async def test_reminders_unknown_kind_rejected(settings_env) -> None:
    handlers, app, _ = settings_env
    token = handlers.auth_token
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/v1/reminders?token={token}",
            json={"kind": "weekly", "message": "x"},
        )
        assert resp.status == 400


# ----------------------------------------------------------------------
# Event sources
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_sources_create_reload_delete(settings_env) -> None:
    handlers, app, root = settings_env
    token = handlers.auth_token

    async with TestClient(TestServer(app)) as client:
        # 先得有工作空间
        ws_dir = root._fixtures.tmp_path / "ws-ev"
        ws_dir.mkdir()
        root.workspace_manager.add_workspace(ws_dir, name="ws-ev")

        payload = {
            "id": "demo-file-watch",
            "enabled": True,
            "kind": "file_watch",
            "workspace": "ws-ev",
            "watch_path": "feedback/inbox",
            "watch_pattern": "*.json",
            "watch_events": ["created"],
            "cooldown_seconds": 30,
        }
        create = await client.post(f"/api/v1/event-sources?token={token}", json=payload)
        assert create.status == 200, await create.text()
        # 写盘后第一次 reload
        assert len(root._fixtures.reload_calls) == 1

        # 列表
        rows = await (await client.get(f"/api/v1/event-sources?token={token}")).json()
        ids = {s["id"] for s in rows["event_sources"]}
        assert "demo-file-watch" in ids

        # 启停
        toggle = await client.post(
            f"/api/v1/event-sources/demo-file-watch/toggle?token={token}",
            json={"enabled": False},
        )
        assert toggle.status == 200
        # toggle 也触发 reload
        assert len(root._fixtures.reload_calls) == 2

        # 删除
        delete = await client.delete(f"/api/v1/event-sources/demo-file-watch?token={token}")
        assert delete.status == 200
        rows = await (await client.get(f"/api/v1/event-sources?token={token}")).json()
        assert "demo-file-watch" not in {s["id"] for s in rows["event_sources"]}


@pytest.mark.asyncio
async def test_event_sources_invalid_id_rejected(settings_env) -> None:
    handlers, app, _ = settings_env
    token = handlers.auth_token
    async with TestClient(TestServer(app)) as client:
        # 路径注入
        resp = await client.delete(f"/api/v1/event-sources/..%2Fevil?token={token}")
        # aiohttp 解码后是 "../evil"，应用层会 400
        assert resp.status == 400


@pytest.mark.asyncio
async def test_event_sources_invalid_payload_rejected(settings_env) -> None:
    handlers, app, root = settings_env
    token = handlers.auth_token
    ws_dir = root._fixtures.tmp_path / "ws-ev2"
    ws_dir.mkdir()
    root.workspace_manager.add_workspace(ws_dir, name="ws-ev2")

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/v1/event-sources?token={token}",
            json={
                "id": "bad",
                "kind": "no_such_kind",
                "workspace": "ws-ev2",
            },
        )
        assert resp.status == 400


# ----------------------------------------------------------------------
# Workspaces
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspaces_crud(settings_env) -> None:
    handlers, app, root = settings_env
    token = handlers.auth_token

    with tempfile.TemporaryDirectory() as td:
        ws_a = Path(td) / "a"
        ws_a.mkdir()
        ws_b = Path(td) / "b"
        ws_b.mkdir()

        async with TestClient(TestServer(app)) as client:
            # add a
            r1 = await client.post(
                f"/api/v1/workspaces?token={token}",
                json={"path": str(ws_a), "name": "ws-a", "summary": "first"},
            )
            assert r1.status == 200, await r1.text()
            assert "ws-a" in {w["name"] for w in root._fixtures.workspaces.values()}

            # add b
            r2 = await client.post(
                f"/api/v1/workspaces?token={token}",
                json={"path": str(ws_b), "name": "ws-b"},
            )
            assert r2.status == 200

            # list
            rows = await (await client.get(f"/api/v1/workspaces?token={token}")).json()
            assert len(rows["workspaces"]) == 2

            # rename
            wid = next(iter(root._fixtures.workspaces))
            rename = await client.post(
                f"/api/v1/workspaces/{wid}/rename?token={token}",
                json={"name": "renamed"},
            )
            assert rename.status == 200
            assert root._fixtures.workspaces[wid]["name"] == "renamed"

            # set default
            other = [w for w in root._fixtures.workspaces if w != wid][0]
            default_resp = await client.post(f"/api/v1/workspaces/{other}/default?token={token}")
            assert default_resp.status == 200
            assert root.workspace_manager.registry.document.default_workspace == other

            # delete
            delete = await client.delete(f"/api/v1/workspaces/{wid}?token={token}")
            assert delete.status == 200
            assert wid not in root._fixtures.workspaces


@pytest.mark.asyncio
async def test_workspaces_add_missing_dir_rejected(settings_env) -> None:
    handlers, app, _ = settings_env
    token = handlers.auth_token
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/v1/workspaces?token={token}",
            json={"path": "D:/does/not/exist/anywhere"},
        )
        assert resp.status == 400

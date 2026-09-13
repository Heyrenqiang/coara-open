"""WebUI 设置中心 CRUD handlers（reminders / event-sources / workspaces）。

设计：扩展既有 ``/config`` 设置中心，对外暴露三大域的 Web CRUD API。
所有写操作走 Root 上的活 service（同进程），即时生效；事件源 YAML 写后
调 ``EventSourceManager.reload()`` 重建运行时。读侧也可直接落盘读取，
以便前端在服务未启时也能列表展示。

API 一览：
- ``GET /api/v1/reminders`` + ``POST /api/v1/reminders`` + ``POST /api/v1/reminders/{id}/toggle``
  + ``DELETE /api/v1/reminders/{id}``
- ``GET /api/v1/event-sources`` + ``POST /api/v1/event-sources`` + ``PUT /api/v1/event-sources/{id}``
  + ``POST /api/v1/event-sources/{id}/toggle`` + ``DELETE /api/v1/event-sources/{id}``
  + ``POST /api/v1/event-sources/reload``
- ``GET /api/v1/workspaces`` + ``POST /api/v1/workspaces`` + ``POST /api/v1/workspaces/{id}/rename``
  + ``POST /api/v1/workspaces/{id}/default`` + ``DELETE /api/v1/workspaces/{id}``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import web

from src.core.errors import ConfigError
from src.core.logger import logger
from src.event_sources.types import EventSourceDefinition
from src.ui.dashboard_tokens import load_or_create_dashboard_token
from src.ui.handlers.base import DashboardAuthMixin


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    """把 WorkspaceEntry 或 duck-typed 对象转可 JSON 序列化的 dict。"""
    if hasattr(entry, "model_dump"):
        return entry.model_dump(mode="json")
    if hasattr(entry, "__dict__"):
        return {k: v for k, v in vars(entry).items() if not k.startswith("_")}
    return dict(entry)


class SettingsHandlers(DashboardAuthMixin):
    """Web 设置中心 CRUD handlers。需要 RootCoara 引用以调用活 service。"""

    def __init__(
        self,
        workspace_dir: Path | str,
        *,
        coara_home: Path | str | None = None,
        root: Any | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()
        self.coara_home = Path(coara_home).expanduser().resolve() if coara_home else None
        # Root 提供 reminder_service / event_source_manager / workspace_manager
        self._root = root
        self.auth_token = auth_token or load_or_create_dashboard_token(self.workspace_dir, self.coara_home)

    # ------------------------------------------------------------------
    # 路由注册
    # ------------------------------------------------------------------

    def register_routes(self, router: web.UrlDispatcher) -> None:
        r = router
        r.add_get("/api/v1/reminders", self.handle_list_reminders)
        r.add_post("/api/v1/reminders", self.handle_create_reminder)
        r.add_post("/api/v1/reminders/{rid}/toggle", self.handle_toggle_reminder)
        r.add_delete("/api/v1/reminders/{rid}", self.handle_delete_reminder)

        r.add_get("/api/v1/event-sources", self.handle_list_event_sources)
        r.add_post("/api/v1/event-sources", self.handle_create_event_source)
        r.add_put("/api/v1/event-sources/{eid}", self.handle_update_event_source)
        r.add_post("/api/v1/event-sources/{eid}/toggle", self.handle_toggle_event_source)
        r.add_delete("/api/v1/event-sources/{eid}", self.handle_delete_event_source)
        r.add_post("/api/v1/event-sources/reload", self.handle_reload_event_sources)

        r.add_get("/api/v1/workspaces", self.handle_list_workspaces)
        r.add_post("/api/v1/workspaces", self.handle_create_workspace)
        r.add_post("/api/v1/workspaces/{wid}/rename", self.handle_rename_workspace)
        r.add_post("/api/v1/workspaces/{wid}/default", self.handle_default_workspace)
        r.add_delete("/api/v1/workspaces/{wid}", self.handle_delete_workspace)

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------

    async def _require_root(self) -> Any:
        if self._root is None:
            raise web.HTTPServiceUnavailable(text="Root 服务未连接，CRUD 不可用")
        return self._root

    async def _json_body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="payload 必须是对象")
        return data

    async def _safe(self, coro):
        """Await a coroutine and map domain errors to HTTP responses."""
        try:
            return await coro
        except web.HTTPException:
            raise
        except ConfigError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except Exception as exc:
            logger.exception("Settings handler failure")
            raise web.HTTPInternalServerError(text=str(exc)) from exc

    # ==================================================================
    # Reminders
    # ==================================================================

    async def handle_list_reminders(self, request: web.Request) -> web.Response:
        self._check_token(request)
        root = await self._require_root()
        try:
            rows = await root.reminder_service.list_reminders()
        except AttributeError:
            rows = []
        return web.json_response({"reminders": rows})

    async def handle_create_reminder(self, request: web.Request) -> web.Response:
        self._check_token(request)
        root = await self._require_root()
        data = await self._json_body(request)
        kind = str(data.get("kind") or "one_time").strip().lower()
        message = str(data.get("message") or "提醒")
        svc = root.reminder_service
        if kind == "one_time":
            msg = await self._safe(
                svc.add_one_time_reminder(
                    message,
                    minutes=int(data.get("minutes") or 0),
                    hours=int(data.get("hours") or 0),
                    days=int(data.get("days") or 0),
                )
            )
        elif kind == "interval":
            msg = await self._safe(
                svc.add_interval_reminder(
                    message,
                    minutes=int(data.get("minutes") or 0),
                    hours=int(data.get("hours") or 0),
                    days=int(data.get("days") or 0),
                )
            )
        elif kind == "cron":
            msg = await self._safe(svc.add_cron_reminder(str(data.get("cron") or ""), message))
        else:
            raise web.HTTPBadRequest(text=f"未知 kind: {kind}")
        return web.json_response({"ok": True, "message": msg})

    async def handle_toggle_reminder(self, request: web.Request) -> web.Response:
        self._check_token(request)
        root = await self._require_root()
        rid = request.match_info["rid"]
        data = await self._json_body(request)
        enabled = bool(data.get("enabled", True))
        msg = await self._safe(root.reminder_service.set_reminder_enabled(rid, enabled))
        return web.json_response({"ok": True, "message": msg, "enabled": enabled})

    async def handle_delete_reminder(self, request: web.Request) -> web.Response:
        self._check_token(request)
        root = await self._require_root()
        rid = request.match_info["rid"]
        msg = await self._safe(root.reminder_service.remove_reminder(rid))
        return web.json_response({"ok": True, "message": msg})

    # ==================================================================
    # Event Sources
    # ==================================================================

    def _es_registry(self) -> Any:
        """事件源定义的 registry（空间布局解析）；未启用时返回 None（回退旧目录）。"""
        try:
            return self._ws_manager().registry
        except Exception:
            return None

    def _es_manager(self) -> Any:
        if self._root is None or not hasattr(self._root, "event_source_manager"):
            raise web.HTTPServiceUnavailable(text="EventSourceManager 未启动")
        return self._root.event_source_manager

    async def handle_list_event_sources(self, request: web.Request) -> web.Response:
        self._check_token(request)
        try:
            mgr = self._es_manager()
            rows = mgr.list_status()
        except web.HTTPException:
            raise
        except Exception:
            rows = []
        return web.json_response({"event_sources": rows})

    async def handle_create_event_source(self, request: web.Request) -> web.Response:
        self._check_token(request)
        data = await self._json_body(request)
        await self._safe(self._write_event_source_yaml(data, overwrite=False))
        await self._safe(self._reload_event_sources())
        return web.json_response({"ok": True})

    async def handle_update_event_source(self, request: web.Request) -> web.Response:
        self._check_token(request)
        eid = request.match_info["eid"]
        data = await self._json_body(request)
        if str(data.get("id") or "") != eid:
            raise web.HTTPBadRequest(text="path 与 body 的 id 不一致")
        await self._safe(self._write_event_source_yaml(data, overwrite=True))
        await self._safe(self._reload_event_sources())
        return web.json_response({"ok": True})

    async def handle_toggle_event_source(self, request: web.Request) -> web.Response:
        self._check_token(request)
        eid = request.match_info["eid"]
        data = await self._json_body(request)
        enabled = bool(data.get("enabled", True))
        defn = await self._safe(self._read_event_source_yaml(eid))
        defn.enabled = enabled
        await self._safe(self._write_event_source_yaml(defn.model_dump(), overwrite=True))
        await self._safe(self._reload_event_sources())
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_delete_event_source(self, request: web.Request) -> web.Response:
        self._check_token(request)
        eid = request.match_info["eid"]
        from src.event_sources.ops import EventSourceOpsError, delete_definition

        if self.coara_home is None:
            raise web.HTTPServiceUnavailable(text="coara_home 未配置")
        try:
            await asyncio.to_thread(delete_definition, self.coara_home, eid, self._es_registry())
        except EventSourceOpsError as exc:
            msg = str(exc)
            if "不存在" in msg:
                raise web.HTTPNotFound(text=msg) from exc
            raise web.HTTPBadRequest(text=msg) from exc
        await self._safe(self._reload_event_sources())
        return web.json_response({"ok": True})

    async def handle_reload_event_sources(self, request: web.Request) -> web.Response:
        self._check_token(request)
        await self._safe(self._reload_event_sources())
        return web.json_response({"ok": True})

    async def _read_event_source_yaml(self, eid: str) -> EventSourceDefinition:
        from src.event_sources.ops import EventSourceOpsError, read_definition

        if self.coara_home is None:
            raise web.HTTPServiceUnavailable(text="coara_home 未配置")
        try:
            return await asyncio.to_thread(read_definition, self.coara_home, eid, self._es_registry())
        except EventSourceOpsError as exc:
            msg = str(exc)
            if "不存在" in msg:
                raise web.HTTPNotFound(text=msg) from exc
            raise web.HTTPBadRequest(text=msg) from exc

    async def _write_event_source_yaml(self, data: dict[str, Any] | EventSourceDefinition, *, overwrite: bool) -> None:
        from src.event_sources.ops import EventSourceOpsError, write_definition

        if self.coara_home is None:
            raise web.HTTPServiceUnavailable(text="coara_home 未配置")
        try:
            await asyncio.to_thread(
                write_definition,
                self.coara_home,
                data,
                overwrite=overwrite,
                registry=self._es_registry(),
            )
        except EventSourceOpsError as exc:
            msg = str(exc)
            if "已存在" in msg:
                raise web.HTTPConflict(text=msg) from exc
            raise web.HTTPBadRequest(text=msg) from exc

    async def _reload_event_sources(self) -> None:
        from src.event_sources.ops import EventSourceOpsError, reload_manager

        try:
            await reload_manager(self._es_manager())
        except EventSourceOpsError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc

    # ==================================================================
    # Workspaces
    # ==================================================================

    def _ws_manager(self) -> Any:
        if self._root is None or not hasattr(self._root, "workspace_manager"):
            raise web.HTTPServiceUnavailable(text="WorkspaceManager 未启动")
        return self._root.workspace_manager

    async def handle_list_workspaces(self, request: web.Request) -> web.Response:
        self._check_token(request)
        try:
            mgr = self._ws_manager()
            entries = mgr.registry.list_active()
            default_id = mgr.registry.document.default_workspace
        except web.HTTPException:
            raise
        except Exception:
            entries, default_id = [], None
        rows = [{**_entry_to_dict(e), "is_default": getattr(e, "id", None) == default_id} for e in entries]
        return web.json_response({"workspaces": rows, "default": default_id})

    async def handle_create_workspace(self, request: web.Request) -> web.Response:
        self._check_token(request)
        data = await self._json_body(request)
        path_str = str(data.get("path") or "").strip()
        if not path_str:
            raise web.HTTPBadRequest(text="path 不能为空")
        name = str(data.get("name") or "").strip() or None
        summary = str(data.get("summary") or "").strip() or None
        mgr = self._ws_manager()
        path = Path(path_str).expanduser().resolve()
        if not path.exists():
            raise web.HTTPBadRequest(text=f"目录不存在: {path}")
        entry = await self._safe(asyncio.to_thread(mgr.add_workspace, path, name=name, summary=summary))
        return web.json_response({"ok": True, "workspace": _entry_to_dict(entry)})

    async def handle_rename_workspace(self, request: web.Request) -> web.Response:
        self._check_token(request)
        wid = request.match_info["wid"]
        data = await self._json_body(request)
        new_name = str(data.get("name") or "").strip()
        if not new_name:
            raise web.HTTPBadRequest(text="name 不能为空")
        mgr = self._ws_manager()
        entry = await self._safe(asyncio.to_thread(mgr.rename_workspace, wid, new_name))
        if entry is None:
            raise web.HTTPNotFound(text=f"找不到工作空间: {wid}")
        return web.json_response({"ok": True, "workspace": _entry_to_dict(entry)})

    async def handle_default_workspace(self, request: web.Request) -> web.Response:
        self._check_token(request)
        wid = request.match_info["wid"]
        mgr = self._ws_manager()
        ok = await self._safe(asyncio.to_thread(mgr.set_persistent_default, wid))
        if not ok:
            raise web.HTTPNotFound(text=f"找不到工作空间: {wid}")
        return web.json_response({"ok": True, "default": wid})

    async def handle_delete_workspace(self, request: web.Request) -> web.Response:
        self._check_token(request)
        wid = request.match_info["wid"]
        mgr = self._ws_manager()
        ok = await self._safe(asyncio.to_thread(mgr.remove_workspace, wid))
        if not ok:
            raise web.HTTPNotFound(text=f"找不到工作空间: {wid}")
        return web.json_response({"ok": True, "deleted": wid})

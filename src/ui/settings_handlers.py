"""WebUI 设置中心 CRUD handlers（reminders / event-sources / workspaces）"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from src.core.errors import ConfigError
from src.core.logger import logger
from src.event_sources.types import EventSourceDefinition
from src.ui.dashboard_tokens import load_or_create_dashboard_token
from src.ui.handlers.base import DashboardAuthMixin
from src.workspace.registry import WorkspaceRegistryConflictError


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    """把 WorkspaceEntry 或 duck-typed 对象转可 JSON 序列化的 dict。"""
    if hasattr(entry, "model_dump"):
        return entry.model_dump(mode="json")
    if hasattr(entry, "__dict__"):
        return {k: v for k, v in vars(entry).items() if not k.startswith("_")}
    return dict(entry)


def _query_flag(value: str | None) -> bool:
    return bool(value) and str(value).strip().lower() in {"1", "true", "yes", "on"}


def _remove_disk_dir(disk_path: Path, coara_home: Path) -> str | None:
    """删除工作空间磁盘目录；返回拒绝/失败原因，成功返回 None"""
    import shutil

    if not disk_path.exists():
        return None
    try:
        if disk_path.resolve() == coara_home.resolve():
            return f"拒绝删除 coara_home 本身：{disk_path}"
    except OSError as exc:
        return f"无法解析路径：{disk_path}（{exc}）"
    if not disk_path.is_dir():
        return f"路径不是目录，未删除：{disk_path}"
    try:
        shutil.rmtree(disk_path)
    except OSError as exc:
        return f"{disk_path}（{exc}）"
    return None


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

    # 路由注册

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
        r.add_post("/api/v1/workspaces/{wid}/rebind", self.handle_rebind_workspace)
        r.add_post("/api/v1/workspaces/{wid}/default", self.handle_default_workspace)
        r.add_delete("/api/v1/workspaces/{wid}", self.handle_delete_workspace)

        r.add_get("/api/fs/browse", self.handle_fs_browse)

    # 通用

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

    # Reminders

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

    # Event Sources

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

    # Workspaces

    def _ws_manager(self) -> Any:
        if self._root is None or not hasattr(self._root, "workspace_manager"):
            raise web.HTTPServiceUnavailable(text="WorkspaceManager 未启动")
        return self._root.workspace_manager

    def _ws_mutate(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """在事件循环线程上执行工作空间登记表的同步写，并把异常映射成 HTTP 响应"""
        try:
            return fn(*args, **kwargs)
        except web.HTTPException:
            raise
        except WorkspaceRegistryConflictError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except ConfigError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except Exception as exc:
            logger.exception("Settings handler failure")
            raise web.HTTPInternalServerError(text=str(exc)) from exc

    async def handle_list_workspaces(self, request: web.Request) -> web.Response:
        """活跃工作空间列表——与侧边栏 ``/api/workspace/list`` 同一套 ACTIVE 口径"""
        self._check_token(request)
        from src.workspace.catalog import resolve_workspace_summary
        from src.workspace.types import WorkspaceStatus

        mgr = self._ws_manager()
        default_id = mgr.registry.document.default_workspace
        rows: list[dict[str, Any]] = []
        for entry in mgr.list_workspaces():
            if entry.status != WorkspaceStatus.ACTIVE:
                continue
            missing = False
            if entry.kind.value != "internal":
                try:
                    missing = not entry.resolved_path().is_dir()
                except OSError:
                    missing = True
            rows.append(
                {
                    "id": entry.id,
                    "name": entry.name,
                    "path": str(entry.path),
                    "kind": entry.kind.value,
                    "status": entry.status.value,
                    "summary": resolve_workspace_summary(entry),
                    "home_view": entry.home_view or "",
                    "is_default": entry.id == default_id,
                    "missing": missing,
                }
            )
        rows.sort(key=lambda r: (r.get("kind") == "internal", str(r.get("name") or "").lower()))
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
            # 仅允许在推荐根（coara_home/workspace）下自动建目录，避免 token
            # 持有者对任意路径 mkdir -p
            allowed_root = self._recommended_root().resolve()
            try:
                path.relative_to(allowed_root)
            except ValueError as exc:
                raise web.HTTPBadRequest(text=f"新建目录须落在推荐根下: {allowed_root}（当前: {path}）") from exc
            try:
                await self._safe(asyncio.to_thread(path.mkdir, parents=True, exist_ok=True))
            except OSError as exc:
                raise web.HTTPBadRequest(text=f"无法创建目录: {path}（{exc}）") from exc
        elif not path.is_dir():
            raise web.HTTPBadRequest(text=f"路径已存在但不是目录: {path}")
        entry = self._ws_mutate(mgr.add_workspace, path, name=name, summary=summary)
        return web.json_response({"ok": True, "workspace": _entry_to_dict(entry)})

    async def handle_rename_workspace(self, request: web.Request) -> web.Response:
        self._check_token(request)
        wid = request.match_info["wid"]
        data = await self._json_body(request)
        new_name = str(data.get("name") or "").strip()
        if not new_name:
            raise web.HTTPBadRequest(text="name 不能为空")
        mgr = self._ws_manager()
        entry = self._ws_mutate(mgr.rename_workspace, wid, new_name)
        if entry is None:
            raise web.HTTPNotFound(text=f"找不到工作空间: {wid}")
        return web.json_response({"ok": True, "workspace": _entry_to_dict(entry)})

    async def handle_rebind_workspace(self, request: web.Request) -> web.Response:
        """改绑目录（目录被删、项目挪位后的恢复）：id 不变，历史档案不断链。"""
        self._check_token(request)
        wid = request.match_info["wid"]
        data = await self._json_body(request)
        new_path = str(data.get("path") or "").strip()
        if not new_path:
            raise web.HTTPBadRequest(text="path 不能为空")
        mgr = self._ws_manager()
        entry = self._ws_mutate(mgr.rebind_workspace, wid, new_path)
        if entry is None:
            raise web.HTTPNotFound(text=f"找不到工作空间: {wid}")
        return web.json_response({"ok": True, "workspace": _entry_to_dict(entry)})

    async def handle_default_workspace(self, request: web.Request) -> web.Response:
        self._check_token(request)
        wid = request.match_info["wid"]
        mgr = self._ws_manager()
        ok = self._ws_mutate(mgr.set_persistent_default, wid)
        if not ok:
            raise web.HTTPNotFound(text=f"找不到工作空间: {wid}")
        return web.json_response({"ok": True, "default": wid})

    def _disk_delete_blocker(self, entry: Any) -> str | None:
        """删盘前的硬守卫：该空间有在飞回合的缓存会话时拦下（口径同 ws 工具）。"""
        root = self._root
        cached = root._sessions.get(entry.id) if root is not None else None
        coara = getattr(cached, "coara", None) if cached is not None else None
        if coara is not None and coara.is_turn_busy():
            return f"工作空间 {entry.name} 有正在进行的会话，不能删除磁盘目录；请先在该空间结束回合"
        return None

    async def handle_delete_workspace(self, request: web.Request) -> web.Response:
        """移出登记；``?delete_disk=1`` 时连同磁盘目录一并删除（不可逆）。"""
        self._check_token(request)
        wid = request.match_info["wid"]
        delete_disk = _query_flag(request.query.get("delete_disk"))
        mgr = self._ws_manager()
        entry = mgr.registry.resolve_name_or_id(wid)
        if entry is None:
            raise web.HTTPNotFound(text=f"找不到工作空间: {wid}")
        if delete_disk:
            blocker = self._disk_delete_blocker(entry)
            if blocker:
                raise web.HTTPBadRequest(text=blocker)
        ok = self._ws_mutate(mgr.remove_workspace, wid)
        if not ok:
            raise web.HTTPBadRequest(text=f"不能移出当前活动工作空间 {entry.name}；请先切换到其他工作空间")
        if not delete_disk:
            return web.json_response(
                {
                    "ok": True,
                    "deleted": entry.id,
                    "disk_deleted": False,
                    "message": f"已移出登记 {entry.name}（磁盘目录保留）",
                }
            )
        disk_path = entry.resolved_path()
        failure = await self._safe(asyncio.to_thread(_remove_disk_dir, disk_path, Path(mgr.coara_home)))
        if failure:
            return web.json_response(
                {
                    "ok": True,
                    "deleted": entry.id,
                    "disk_deleted": False,
                    "message": f"已移出登记 {entry.name}，但磁盘未删除：{failure}",
                }
            )
        return web.json_response(
            {
                "ok": True,
                "deleted": entry.id,
                "disk_deleted": True,
                "message": f"已移出登记并删除磁盘目录：{disk_path}",
            }
        )

    # 目录浏览（只读：web 添加工作空间的目录选择器）

    def _recommended_root(self) -> Path:
        """推荐根目录＝coara_home 下的 workspace 目录（新建空间默认落点）。"""
        if self.coara_home is not None:
            root = self.coara_home / "workspace"
            root.mkdir(parents=True, exist_ok=True)
            return root
        paths: list[Path] = []
        try:
            mgr = self._ws_manager()
            for entry in mgr.registry.list_active():
                raw = getattr(entry, "path", None)
                if not raw:
                    continue
                try:
                    p = Path(str(raw)).expanduser().resolve()
                except Exception:
                    continue
                if p.exists():
                    paths.append(p)
        except Exception:
            paths = []
        if not paths:
            return Path(os.getcwd())
        try:
            return Path(os.path.commonpath([str(p) for p in paths]))
        except ValueError:
            # 跨盘符（Windows 多 drive）无公共路径，退第一个空间的父目录
            return paths[0].parent

    async def handle_fs_browse(self, request: web.Request) -> web.Response:
        """GET /api/fs/browse?path=<绝对路径> —— 只列目录，path 空时浏览推荐根目录。"""
        self._check_token(request)
        raw = str(request.query.get("path") or "").strip()
        if not raw:
            target = self._recommended_root()
        else:
            expanded = Path(raw).expanduser()
            if not expanded.is_absolute():
                raise web.HTTPBadRequest(text="path 必须是绝对路径")
            target = expanded
        if not target.is_dir():
            raise web.HTTPBadRequest(text=f"目录不存在或不可读: {target}")
        try:
            resolved = target.resolve()
        except Exception:
            resolved = target
        dirs: list[dict[str, str]] = []
        try:
            entries = await asyncio.to_thread(lambda: list(os.scandir(resolved)))
        except PermissionError as exc:
            raise web.HTTPForbidden(text=f"无权限读取: {resolved}") from exc
        for e in entries:
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            dirs.append({"name": e.name, "path": str(Path(e.path))})
        dirs.sort(key=lambda d: d["name"].lower())
        parent = resolved.parent
        return web.json_response(
            {
                "path": str(resolved),
                "parent": str(parent) if parent != resolved else None,
                "dirs": dirs,
            }
        )

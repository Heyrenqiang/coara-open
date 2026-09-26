from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from src.ui.handler_contract import HandlerMixinBase


class UsageRecordsHandlers(HandlerMixinBase):
    def _records_store_or_none(self) -> Any:
        return getattr(self.root, "records_store", None)

    def _agent_records_store_or_none(self) -> Any:
        store = self._records_store_or_none()
        if store is not None:
            return getattr(store, "agent", None)
        return None

    def _user_records_store_or_none(self) -> Any:
        store = self._records_store_or_none()
        if store is not None:
            return getattr(store, "user", None)
        return None

    @staticmethod
    def _scope_from_request(request: web.Request) -> str:
        scope = str(request.rel_url.query.get("scope") or "agent").strip().lower()
        return scope if scope in ("agent", "user") else "agent"

    def _coara_home(self) -> Path | None:
        """解析 coara_home：优先配置管理器，其次工作空间管理器的登记 home。"""
        from src.core.config import config_manager

        if getattr(config_manager, "_config", None) is not None:
            return Path(config_manager.config.coara_home)
        if self.root is not None:
            wm = getattr(self.root, "workspace_manager", None)
            home = getattr(wm, "coara_home", None) if wm is not None else None
            if home is not None:
                return Path(home)
        return None

    @staticmethod
    def _record_entry_json(entry: Any) -> dict[str, Any]:
        src = getattr(entry, "source", None)
        workspace = str(getattr(src, "workspace", "") or "") if src is not None else ""
        return {
            "id": entry.id,
            "type": entry.type,
            "scope": entry.scope,
            "sensitivity": entry.sensitivity,
            "title": entry.title,
            "content": entry.content,
            "tags": list(entry.tags or []),
            "status": entry.status,
            "source_type": entry.source_type,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "last_accessed": entry.last_accessed.isoformat() if entry.last_accessed else None,
            "access_count": entry.access_count,
            "path": entry.path,
            # 来源空间目录名（录像溯源），手机/web 端条目标记用
            "workspace": workspace,
        }

    @staticmethod
    def _collection_entry_json(entry: Any) -> dict[str, Any]:
        file_name = ""
        file_rel = ""
        if str(getattr(entry, "source_type", "") or "") == "file":
            # content 形如「文件：x\n路径：`files/id/name`\n…」
            for line in str(getattr(entry, "content", "") or "").splitlines():
                line = line.strip()
                if line.startswith("文件："):
                    file_name = line[3:].strip()
                elif line.startswith("路径："):
                    raw = line[3:].strip().strip("`")
                    file_rel = raw
        return {
            "id": entry.id,
            "origin": "user",
            "title": entry.title,
            "summary": entry.summary,
            "note": entry.note,
            "content": entry.content,
            "tags": list(entry.tags or []),
            "source_type": entry.source_type,
            "source_url": entry.source_url,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "last_accessed": entry.last_accessed.isoformat() if entry.last_accessed else None,
            "access_count": entry.access_count,
            "path": entry.path,
            "file_name": file_name,
            "file_rel": file_rel,
            "has_file": bool(file_rel) or str(getattr(entry, "source_type", "") or "") == "file",
        }

    def _resolve_collection_file(self, user_store: Any, entry_id: str) -> Path | None:
        """Resolve binary under records/user/files/<id>/ for download."""
        files_dir = Path(user_store.root) / "files" / entry_id
        if not files_dir.is_dir():
            return None
        for child in sorted(files_dir.iterdir()):
            if child.is_file():
                return child.resolve()
        return None

    async def _handle_usage_dashboard(self, request: web.Request) -> web.Response:
        """Aggregate token usage across workspaces for the Web usage board."""
        self._check_token(request)
        try:
            days = int(request.rel_url.query.get("days") or 7)
        except ValueError:
            days = 7
        days = max(1, min(days, 366))
        workspace_id = str(request.rel_url.query.get("workspace_id") or "").strip() or None
        try:
            recent_limit = int(request.rel_url.query.get("recent_limit") or 80)
        except ValueError:
            recent_limit = 80
        recent_limit = max(1, min(recent_limit, 500))

        from src.core.config import config_manager
        from src.runtime.usage_query import summarize_usage_dashboard

        coara_home = None
        if config_manager._config is not None:
            coara_home = config_manager.config.coara_home
        if coara_home is None and self.root is not None:
            wm = getattr(self.root, "workspace_manager", None)
            if wm is not None and getattr(wm, "coara_home", None) is not None:
                coara_home = wm.coara_home

        payload = await asyncio.to_thread(
            summarize_usage_dashboard,
            days=days,
            workspace_id=workspace_id,
            coara_home=Path(coara_home) if coara_home else None,
            recent_limit=recent_limit,
        )
        return web.json_response(payload)

    async def _handle_usage_range(self, request: web.Request) -> web.Response:
        """任意日区间的三维交叉聚合（手机端日历热图：点选日/整周/整月）。"""
        self._check_token(request)
        date_from = str(request.rel_url.query.get("from") or "").strip()
        date_to = str(request.rel_url.query.get("to") or "").strip()

        from src.core.config import config_manager
        from src.runtime.usage_query import summarize_usage_range

        coara_home = None
        if config_manager._config is not None:
            coara_home = config_manager.config.coara_home
        if coara_home is None and self.root is not None:
            wm = getattr(self.root, "workspace_manager", None)
            if wm is not None and getattr(wm, "coara_home", None) is not None:
                coara_home = wm.coara_home

        payload = await asyncio.to_thread(
            summarize_usage_range,
            date_from=date_from,
            date_to=date_to,
            coara_home=Path(coara_home) if coara_home else None,
        )
        return web.json_response(payload)

    async def _handle_usage_detail(self, request: web.Request) -> web.Response:
        """最近 N 天消费明细：会话 → 大轮 → 小轮（费用按配置价格估算）。"""
        self._check_token(request)
        try:
            days = int(request.rel_url.query.get("days") or 7)
        except ValueError:
            days = 7
        days = max(1, min(days, 366))
        workspace_id = str(request.rel_url.query.get("workspace_id") or "").strip() or None
        try:
            limit = int(request.rel_url.query.get("limit") or 500)
        except ValueError:
            limit = 500
        limit = max(1, min(limit, 2000))

        from src.core.config import config_manager
        from src.runtime.usage_query import summarize_usage_detail

        coara_home = None
        if config_manager._config is not None:
            coara_home = config_manager.config.coara_home
        if coara_home is None and self.root is not None:
            wm = getattr(self.root, "workspace_manager", None)
            if wm is not None and getattr(wm, "coara_home", None) is not None:
                coara_home = wm.coara_home

        payload = await asyncio.to_thread(
            summarize_usage_detail,
            days=days,
            workspace_id=workspace_id,
            coara_home=Path(coara_home) if coara_home else None,
            limit=limit,
        )
        return web.json_response(payload)

    async def _handle_usage_pricing(self, request: web.Request) -> web.Response:
        """模型价格表：每个在用模型的生效价格与来源（config/override），供用量页展示与编辑。"""
        self._check_token(request)
        from src.runtime.pricing_store import list_pricing_entries

        coara_home = self._coara_home()
        entries = await asyncio.to_thread(list_pricing_entries, coara_home)
        return web.json_response({"models": entries})

    async def _handle_usage_pricing_update(self, request: web.Request) -> web.Response:
        """更新某 `provider/model` 的覆盖价；`pricing` 传空/null 表示恢复默认。

        字段级合并：只更新传入字段，其余保留。估算费用随之按新价现算。
        """
        self._check_token(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="无效的 JSON 请求体")
        provider = str(body.get("provider") or "").strip()
        model = str(body.get("model") or "").strip()
        if not provider or not model:
            raise web.HTTPBadRequest(text="缺少 provider/model")
        pricing = body.get("pricing")  # None / 空 dict → 恢复默认（删除覆盖项）
        from src.runtime.pricing_store import save_override

        await asyncio.to_thread(save_override, self._coara_home(), provider, model, pricing)
        return web.json_response({"ok": True, "model_key": f"{provider}/{model}"})

    async def _handle_records_list(self, request: web.Request) -> web.Response:
        """List / search records. ``scope=agent``（默认）或 ``scope=user``。"""
        self._check_token(request)
        scope = self._scope_from_request(request)
        query = str(request.rel_url.query.get("query") or "").strip()
        try:
            limit = int(request.rel_url.query.get("limit") or 50)
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))

        if scope == "user":
            store = self._user_records_store_or_none()
            if store is None:
                return web.json_response({"enabled": False, "scope": "user", "entries": [], "count": 0})
            if query:
                entries = await store.search(query=query, limit=limit)
            else:
                entries = await store.list_entries(limit=limit)
            return web.json_response(
                {
                    "enabled": True,
                    "scope": "user",
                    "entries": [self._collection_entry_json(e) for e in entries],
                    "count": len(entries),
                }
            )

        store = self._agent_records_store_or_none()
        if store is None:
            return web.json_response({"enabled": False, "scope": "agent", "entries": [], "count": 0})
        type_filter = str(request.rel_url.query.get("type") or "").strip() or None
        include_archived = str(request.rel_url.query.get("include_archived") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        if query:
            entries = await store.search(
                query=query,
                type_filter=[type_filter] if type_filter else None,
                include_archived=include_archived,
                limit=limit,
            )
        else:
            entries = await store.list_entries(
                type_filter=type_filter,
                include_archived=include_archived,
                limit=limit,
            )
        return web.json_response(
            {
                "enabled": True,
                "scope": "agent",
                "entries": [self._record_entry_json(e) for e in entries],
                "count": len(entries),
            }
        )

    async def _handle_records_get(self, request: web.Request) -> web.Response:
        self._check_token(request)
        scope = self._scope_from_request(request)
        mid = str(request.rel_url.query.get("id") or "").strip()
        if not mid:
            raise web.HTTPBadRequest(text="缺少 id")

        if scope == "user":
            store = self._user_records_store_or_none()
            if store is None:
                raise web.HTTPNotFound(text="收藏库未初始化")
            entry = await store.read(mid)
            if entry is None:
                raise web.HTTPNotFound(text=f"未找到收藏：{mid}")
            await store.touch(mid)
            return web.json_response({"enabled": True, "scope": "user", "entry": self._collection_entry_json(entry)})

        store = self._agent_records_store_or_none()
        if store is None:
            raise web.HTTPNotFound(text="记录未启用")
        entry = await store.read(mid)
        if entry is None:
            raise web.HTTPNotFound(text=f"未找到记录：{mid}")
        await store.touch(mid)
        return web.json_response({"enabled": True, "scope": "agent", "entry": self._record_entry_json(entry)})

    async def _handle_records_delete(self, request: web.Request) -> web.Response:
        self._check_token(request)
        scope = self._scope_from_request(request)
        mid = str(request.rel_url.query.get("id") or "").strip()
        if not mid:
            raise web.HTTPBadRequest(text="缺少 id")

        if scope == "user":
            store = self._user_records_store_or_none()
            if store is None:
                raise web.HTTPNotFound(text="收藏库未初始化")
            ok = await store.delete(mid)
            if not ok:
                raise web.HTTPNotFound(text=f"未找到收藏：{mid}")
            return web.json_response({"deleted": True, "id": mid, "scope": "user"})

        store = self._agent_records_store_or_none()
        if store is None:
            raise web.HTTPNotFound(text="记录未启用")
        ok = await store.delete(mid)
        if not ok:
            raise web.HTTPNotFound(text=f"未找到记录：{mid}")
        return web.json_response({"deleted": True, "id": mid, "scope": "agent"})

    async def _handle_records_archive(self, request: web.Request) -> web.Response:
        self._check_token(request)
        store = self._agent_records_store_or_none()
        if store is None:
            raise web.HTTPNotFound(text="记录未启用")
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from exc
        mid = str((body or {}).get("id") or "").strip()
        if not mid:
            raise web.HTTPBadRequest(text="缺少 id")
        ok = await store.archive(mid)
        if not ok:
            raise web.HTTPConflict(text=f"无法归档：{mid}")
        return web.json_response({"archived": True, "id": mid})

    async def _handle_records_unarchive(self, request: web.Request) -> web.Response:
        self._check_token(request)
        store = self._agent_records_store_or_none()
        if store is None:
            raise web.HTTPNotFound(text="记录未启用")
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from exc
        mid = str((body or {}).get("id") or "").strip()
        if not mid:
            raise web.HTTPBadRequest(text="缺少 id")
        ok = await store.unarchive(mid)
        if not ok:
            raise web.HTTPConflict(text=f"无法恢复：{mid}")
        return web.json_response({"unarchived": True, "id": mid})

    async def _handle_records_collect(self, request: web.Request) -> web.Response:
        """UI button / API collect → origin=user write (no LLM)."""
        self._check_token(request)
        from src.records.facade import facade_from_root

        facade = facade_from_root(self.root)
        if facade.store is None:
            raise web.HTTPNotFound(text="记录未初始化")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        title = str(body.get("title") or "").strip()
        summary = str(body.get("summary") or "").strip()
        url = str(body.get("url") or "").strip()
        note = str(body.get("note") or "").strip()
        content = str(body.get("content") or "").strip()
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        if not title and url:
            title = url[:120]
        if not summary:
            summary = note or content[:500] or (f"收藏：{url}" if url else "")
        if not title:
            raise web.HTTPBadRequest(text="需要 title 或 url")
        if not summary:
            raise web.HTTPBadRequest(text="需要 summary / content / note")
        result = await facade.add_user(
            title=title,
            summary=summary,
            url=url,
            note=note,
            content=content,
            tags=[str(t) for t in tags if str(t).strip()],
        )
        if result.is_error:
            raise web.HTTPBadRequest(text=result.message)
        return web.json_response({"ok": True, "message": result.message, **(result.metadata or {})})

    async def _handle_records_collect_file(self, request: web.Request) -> web.Response:
        """Web 文件页收藏：工作区文件 → user 收藏库（手点，不经 LLM）。"""
        self._check_token(request)
        from src.records.facade import facade_from_root

        facade = facade_from_root(self.root)
        if facade.store is None or facade.store.user is None:
            raise web.HTTPNotFound(text="记录未初始化")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        raw_path = str(body.get("path") or "").strip()
        note = str(body.get("note") or "").strip()
        if not raw_path:
            raise web.HTTPBadRequest(text="需要 path")

        ws_root = self._view_coara().workspace_dir.resolve()
        target = self._resolve_file_target(raw_path, ws_root)
        if target is None:
            raise web.HTTPForbidden(text=f"路径不可用：{raw_path}")
        if not target.is_file():
            raise web.HTTPBadRequest(text=f"不是文件：{raw_path}")

        max_bytes = 32 * 1024 * 1024  # 32MB
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise web.HTTPBadRequest(text=f"无法读取文件：{exc}") from exc
        if size > max_bytes:
            raise web.HTTPBadRequest(text=f"文件过大（上限 {max_bytes} 字节）")
        try:
            file_bytes = target.read_bytes()
        except OSError as exc:
            raise web.HTTPBadRequest(text=f"无法读取文件：{exc}") from exc

        import mimetypes

        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        ws_name = str(getattr(getattr(self.root, "workspace_manager", None), "active_name", "") or "")
        source_url = f"web:{ws_name or 'ws'}:{raw_path}"
        result = await facade.add_user_file(
            title=target.name,
            summary=note or f"收藏文件：{target.name}",
            filename=target.name,
            file_bytes=file_bytes,
            mime=mime,
            source_url=source_url,
            note=note,
            tags=["web", "file"],
        )
        if result.is_error:
            raise web.HTTPBadRequest(text=result.message)
        return web.json_response({"ok": True, "message": result.message, **(result.metadata or {})})

    async def _handle_records_file(self, request: web.Request) -> web.Response:
        """Download a collected file from records/user/files/<id>/."""
        self._check_token(request)
        mid = str(request.rel_url.query.get("id") or "").strip()
        if not mid:
            raise web.HTTPBadRequest(text="缺少 id")
        store = self._user_records_store_or_none()
        if store is None:
            raise web.HTTPNotFound(text="收藏库未初始化")
        entry = await store.read(mid)
        if entry is None:
            raise web.HTTPNotFound(text=f"未找到收藏：{mid}")
        target = self._resolve_collection_file(store, mid)
        if target is None or not target.is_file():
            raise web.HTTPNotFound(text="该收藏没有可下载文件")
        root = (Path(store.root) / "files" / mid).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise web.HTTPForbidden(text="非法文件路径") from exc
        return web.FileResponse(
            path=target,
            headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
        )

from __future__ import annotations

from aiohttp import web

from src.core.logger import logger
from src.ui.handler_contract import HandlerMixinBase


class UpdatesHandlers(HandlerMixinBase):
    def _updates_store(self):
        getter = getattr(self.root, "_updates_store", None)
        return getter() if getter is not None else None

    async def _push_updates_state_best_effort(self) -> None:
        push = getattr(self.root, "_push_workspace_updates_state_to_matrix", None)
        if push is None:
            return
        try:
            await push()
        except Exception as exc:
            logger.debug(f"updates state push skipped: {exc}")

    async def _handle_updates_summary(self, request: web.Request) -> web.Response:
        """各工作空间未读数 + 跨空间高显著未读消息数（侧栏角标用）。"""
        self._check_token(request)
        store = self._updates_store()
        if store is None:
            return web.json_response({"unread": {}, "pending": 0})
        unread: dict[str, int] = {}
        for name, row in store.stats().items():
            count = int(row.get("unread", 0))
            if count > 0:
                unread[name] = count
        return web.json_response({"unread": unread, "pending": len(store.pending(limit=100))})

    async def _handle_updates_list(self, request: web.Request) -> web.Response:
        """列出收件箱内容：workspace / status / salience 过滤。"""
        self._check_token(request)
        store = self._updates_store()
        if store is None:
            return web.json_response({"items": []})
        workspace = (request.query.get("workspace") or "").strip() or None
        status = request.query.get("status", "unread")
        if status not in ("unread", "read", "archived", "all"):
            status = "unread"
        salience = (request.query.get("salience") or "").strip()
        try:
            limit = max(1, min(500, int(request.query.get("limit", "50"))))
        except ValueError:
            limit = 50
        messages = store.list_messages(workspace=workspace, status=status, limit=limit)
        items = [m.to_dict() for m in messages if not salience or m.salience == salience]
        return web.json_response({"items": items})

    async def _handle_updates_pending(self, request: web.Request) -> web.Response:
        """前台消息视图：跨空间高显著未读。"""
        self._check_token(request)
        store = self._updates_store()
        if store is None:
            return web.json_response({"items": []})
        try:
            limit = max(1, min(200, int(request.query.get("limit", "50"))))
        except ValueError:
            limit = 50
        return web.json_response({"items": [m.to_dict() for m in store.pending(limit=limit)]})

    async def _handle_updates_read(self, request: web.Request) -> web.Response:
        """知道了：标已读。"""
        self._check_token(request)
        body = await request.json()
        store = self._updates_store()
        msg = store.get(str(body.get("message_id") or "")) if store is not None else None
        if store is None or msg is None:
            return web.json_response({"error": "动态不存在"}, status=404)
        store.mark_read(msg.message_id)
        await self._push_updates_state_best_effort()
        return web.json_response({"ok": True})

    async def _handle_updates_archive(self, request: web.Request) -> web.Response:
        """忽略：归档。"""
        self._check_token(request)
        body = await request.json()
        store = self._updates_store()
        msg = store.get(str(body.get("message_id") or "")) if store is not None else None
        if store is None or msg is None:
            return web.json_response({"error": "动态不存在"}, status=404)
        store.archive(msg.message_id)
        await self._push_updates_state_best_effort()
        return web.json_response({"ok": True})

    async def _handle_updates_mark_read(self, request: web.Request) -> web.Response:
        """整空间全部标已读。"""
        self._check_token(request)
        body = await request.json()
        workspace = str(body.get("workspace") or "").strip()
        store = self._updates_store()
        if not workspace or store is None:
            return web.json_response({"error": "需要 workspace"}, status=400)
        store.mark_all_read(workspace)
        await self._push_updates_state_best_effort()
        return web.json_response({"ok": True})

    async def _handle_updates_review(self, request: web.Request) -> web.Response:
        """批示：用户指示连同内容引用注入所属工作空间的会话处理。"""
        self._check_token(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        message_id = str(body.get("message_id") or "").strip()
        text = str(body.get("text") or "").strip()
        if not message_id or not text:
            return web.json_response({"error": "需要 message_id 与 text"}, status=400)
        from src.workspace.updates.review import ReviewError, review_workspace_update

        try:
            result = await review_workspace_update(self.root, message_id=message_id, text=text)
        except ReviewError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.json_response({"ok": True, **result})

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from src.core.logger import logger
from src.ui.handler_contract import HandlerMixinBase
from src.ui.handlers.session import _DEFAULT_HISTORY_LIMIT, _MAX_HISTORY_LIMIT
from src.ui.trace_recording import subscribe_trace_persistence
from src.ui.trace_store import TraceStore


class WorkspaceHandlers(HandlerMixinBase):
    # 宿主 WebServer 提供的 trace 持久化订阅句柄（组合后才有）
    _trace_persistence_sub: Any | None

    async def _handle_workspace_list(self, request: web.Request) -> web.Response:
        """List all registered workspaces with active marker = **web view**."""
        self._check_token(request)
        manager = self.root.workspace_manager
        if manager is None:
            return web.json_response({"workspaces": [], "active_name": None})
        from src.workspace.catalog import resolve_workspace_summary

        view_id = self.root.view_workspace_id("web")
        active_name = ""
        entry_by_id = {e.id: e for e in manager.list_workspaces()}
        if view_id and view_id in entry_by_id:
            active_name = entry_by_id[view_id].name
        elif view_id:
            try:
                view_coara = self.root.resolve_view_coara("web")
            except RuntimeError:
                view_coara = None
            if view_coara is not None:
                from src.workspace.catalog import resolve_foreground_active_name

                active_name = resolve_foreground_active_name(view_coara, manager) or ""

        from src.workspace.identity import resolve_home_view

        workspaces = []
        for entry in manager.list_workspaces():
            # 门面形态：条目显式声明优先，否则走覆盖链（space.yaml → 类型默认 → 仓库）
            if entry.storefront:
                storefront = entry.storefront
            else:
                storefront = resolve_home_view(entry.resolved_path(), content_type=entry.content_type).value
            # 目录被删检测：internal 系统空间的目录是占位（.internal 槽位，永远该在），
            # missing 只标记用户对话空间——侧边栏据此置灰并引导恢复。
            missing = False
            if entry.kind.value != "internal":
                try:
                    missing = not entry.resolved_path().is_dir()
                except OSError:
                    missing = True
            workspaces.append(
                {
                    "name": entry.name or "",
                    "summary": resolve_workspace_summary(entry),
                    "path": str(entry.path),
                    "mode": entry.mode.value,
                    "kind": entry.kind.value,
                    "active": entry.id == view_id,
                    "storefront": storefront,
                    "home_view": entry.home_view or "",
                    "missing": missing,
                }
            )
        return web.json_response(
            {
                "workspaces": workspaces,
                "active_name": active_name or None,
            }
        )

    def _wire_outbound_file_bridge(self) -> None:
        """Attach Web delivery to the shared ``send_file`` router.

        Safe alongside Matrix: ``set_web`` never clears ``set_matrix``.
        """
        from src.tools.builtin.integration.outbound_file import (
            ensure_outbound_file_router,
            register_send_file_on_root,
        )
        from src.ui.web_file_bridge import WebFileBridge

        if self.coara_home is not None:
            delivery_dir = Path(self.coara_home) / "outbound_files"
        else:
            delivery_dir = self.workspace_dir / ".coara" / "outbound_files"

        def _web_view_ws_id() -> str | None:
            ws = getattr(self.root, "web_view_workspace_id", None) or getattr(self.root, "_foreground_session_id", None)
            return str(ws) if ws else None

        bridge = WebFileBridge(
            registry=self.registry,
            delivery_dir=delivery_dir,
            persist=self._persist_outbound_file,
            coara_home=self.coara_home,
            workspace_id_getter=_web_view_ws_id,
        )
        self._web_file_bridge = bridge
        router = ensure_outbound_file_router(self.root)
        router.set_web(bridge)
        try:
            ws_root = self.root.foreground_coara.workspace_dir
        except Exception:
            ws_root = self.workspace_dir
        register_send_file_on_root(self.root, workspace_root=ws_root, router=router)
        logger.info("[Web] Remote file tool enabled: send_file → browser")

    async def _handle_workspace_switch(self, request: web.Request) -> web.Response:
        """Switch active workspace by alias（响应直接带回目标空间的权威快照）。

        端上切空间只需要这一次往返：切完就地用响应里的 snapshot 一次提交上屏，
        不必再发 /api/session/messages。snapshot 与快照接口同源同形状（同一个
        构造点 `_load_view_snapshot`），含 messages / latest_seq / epoch /
        workspace_dir / session_id / subagent_results / runtime。

        body 可选 ``limit``（默认 ``_DEFAULT_HISTORY_LIMIT``=100，上限 500）——直接
        复用 ``handlers/session.py`` 的两个常量，本文件不另写数。客户端 hydrate
        另传 200 并不构成第二份口径：同一 epoch 的同一条线、同一个 view_seq 体系，
        只是分页大小不同（页大小只决定首屏行数，端上按 latest_seq/epoch 对账）。
        """
        self._check_token(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        workspace = str(body.get("name") or "").strip()
        if not workspace:
            return web.json_response({"error": "工作空间名不能为空"}, status=400)
        try:
            limit = max(1, min(_MAX_HISTORY_LIMIT, int(body.get("limit", _DEFAULT_HISTORY_LIMIT))))
        except (TypeError, ValueError):
            limit = _DEFAULT_HISTORY_LIMIT

        # 目录被删的空间禁止切入：对着幽灵目录开会话是静默事故。返回 missing 标记，
        # 前端据此走恢复流程（改绑路径 / 从登记册移除），internal 系统空间豁免。
        entry = None
        ws_mgr = getattr(self.root, "workspace_manager", None)
        if ws_mgr is not None:
            entry = ws_mgr.registry.resolve_name_or_id(workspace)
        if entry is not None and entry.kind.value != "internal":
            try:
                if not entry.resolved_path().is_dir():
                    return web.json_response(
                        {"error": f"空间目录不存在：{entry.path}", "missing": True, "workspace": entry.name},
                        status=409,
                    )
            except OSError:
                return web.json_response(
                    {"error": f"空间目录不可访问：{entry.path}", "missing": True, "workspace": entry.name},
                    status=409,
                )

        # web 独立视图（D6）：浏览器切空间只换本端视图，不动全局前台（不影响
        # CLI/matrix）。set_web_view_workspace 无前台副作用（vault/占用/cwd）。
        success = await self.root.set_web_view_workspace(workspace)
        if not success:
            return web.json_response({"error": f"切换到 {workspace} 失败"}, status=400)
        # 换视图后本地刷新 trace_store/workspace_dir 并推 state（不再依赖全局
        # workspace_switched 事件——视图切换不发该事件）。
        view = self.root.resolve_web_view_coara()
        self.workspace_dir = view.workspace_dir
        self._refresh_trace_store()
        if self.registry.has_active():
            runtime = self._build_lightweight_runtime()
            if runtime is not None:
                await self.registry.send_to_active({"type": "state", "data": {"runtime": runtime}})
        # 快照必须在 set_web_view_workspace + _refresh_trace_store 之后取：此刻
        # self.workspace_dir 已是目标空间，读到的是目标空间那条线。
        snapshot = await self._load_view_snapshot("web", limit=limit)
        return web.json_response(
            {
                "name": workspace,
                "workspace_dir": str(view.workspace_dir),
                "session_id": view.session_id,
                "snapshot": snapshot,
            }
        )

    # ------------------------------------------------------------------
    # Updates inbox REST endpoints（消息中心）
    # ------------------------------------------------------------------

    def _on_web_followup_turn_end(self, event: Any) -> None:
        """web 跟话参与的它端回合结束：web 视图补 turn_end 帧 + 清跟话视图标记。

        刷新一致性铁律：web 端显示过的内容必须落视图存储。web 跟话在它端回合
        里写入了回合起点/user/chunk/diff 帧，回合结束需补 turn_end（否则 build_messages
        把该回合标 recovered「中断」，与实时不符）。

        匹配顺序：先按 event.turn_id 精确命中 followup 流；未命中时再按 session_id
        收尾该会话上所有未结束的 followup-web 流（注入瞬间快照的 turn_id 可能与
        收尾事件不一致：空快照、接续 leftover 换号、或 turn_end 早于流创建）。
        """
        payload = getattr(event, "payload", None) or {}
        origin_scope = str(payload.get("origin_scope") or "main_loop")
        if origin_scope not in ("main_loop", ""):
            return
        turn_id = str(payload.get("turn_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not turn_id and not session_id:
            return

        # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_view_store 可能不存在
        view_store = getattr(self, "_view_store", None)
        reason = str(payload.get("reason") or "complete")
        ended_via_stream = False
        closed_sids: set[str] = set()

        def _is_open_followup(stream: Any) -> bool:
            if str(getattr(getattr(stream, "route", None), "channel_id", "") or "") != "followup-web":
                return False
            return not getattr(stream, "done", True)

        exact: list[Any] = []
        by_session: list[Any] = []
        # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_turns 可能不存在
        for stream in list(getattr(self, "_turns", {}).values()):
            if not _is_open_followup(stream):
                continue
            stream_tid = str(getattr(stream, "turn_id", "") or "")
            stream_sid = str(getattr(stream, "session_id", "") or "")
            if turn_id and stream_tid == turn_id:
                exact.append(stream)
            elif session_id and stream_sid == session_id:
                by_session.append(stream)

        for stream in exact or by_session:
            stream.emit("turn_end", reason=reason)
            stream.finish()
            ended_via_stream = True
            sid = str(getattr(stream, "session_id", "") or "")
            if sid:
                closed_sids.add(sid)

        # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，该集合可能不存在
        for sess, tid in list(getattr(self, "_web_followup_view_turns", set())):
            clear = bool(turn_id and tid == turn_id or not exact and session_id and sess == session_id)
            if not clear:
                continue
            if view_store is not None and not ended_via_stream and turn_id:
                try:
                    tape_ws = getattr(self, "workspace_dir", None)
                    # 有意 getattr 防御：同上（__new__ 测试夹具）
                    for stream in getattr(self, "_turns", {}).values():
                        if str(getattr(stream, "session_id", "") or "") != sess:
                            continue
                        stream_ws = str(getattr(stream, "workspace_dir", "") or "").strip()
                        if stream_ws:
                            tape_ws = stream_ws
                            break
                    from src.ui.view_recorder import record_view_frame

                    record_view_frame(
                        {
                            "kind": "turn_end",
                            "turn_id": turn_id,
                            "source": "web",
                            "subject": "root",
                            "session_id": sess,
                            "workspace_dir": str(tape_ws or self.workspace_dir or ""),
                            "payload": {"reason": reason},
                        },
                        coara_home=self.coara_home,
                    )
                except Exception:
                    logger.warning(
                        "turn_end follow-up frame failed to record to view tape, tape may miss a frame", exc_info=True
                    )
            self._web_followup_view_turns.discard((sess, tid))
            closed_sids.add(sess)

        for sess in closed_sids:
            end_registry = getattr(getattr(self, "root", None), "end_registry", None)
            if end_registry is not None:
                sender = end_registry.sender_for("web", sess)
                if sender is not None:
                    end_registry.unregister("web", sender, sess)

    def _on_workspace_switched_event(self, event: Any) -> None:
        """Handle workspace_switched events from the EventBus.

        When a workspace switch is initiated from CLI (/ws switch) or LLM
        (ws tool), this server's ``trace_store`` and ``workspace_dir``
        still point at the OLD workspace. This callback refreshes them
        so REST handlers read from the correct workspace's data dir.

        This is critical for CLI+Web mode: without it, the browser would
        display stale messages from the previous workspace for up to 5s
        (heartbeat delay) and REST calls would read wrong data.
        (Subscribed by exact topic — no event_type re-check needed.)
        """
        # web 已 pin（启动即 pin）：CLI/matrix 的 workspace_switched 不牵动 web。
        pinned = getattr(self.root, "pinned_view_id", None)
        if callable(pinned) and pinned("web"):
            return
        payload = getattr(event, "payload", None) or {}
        new_dir = payload.get("workspace_dir", "")
        if new_dir and str(self.root.foreground_coara.workspace_dir) != new_dir:
            root_dir = self.root.foreground_coara.workspace_dir
            logger.warning(f"workspace_switched event dir mismatch: root={root_dir} vs event={new_dir}")
        self.workspace_dir = self._view_coara().workspace_dir
        self._refresh_trace_store()
        if self.registry.has_active():
            runtime = self._build_lightweight_runtime()
            if runtime is not None:
                self._spawn_bg_task(self.registry.send_to_active({"type": "state", "data": {"runtime": runtime}}))

    def _on_llm_switched_event(self, event: Any) -> None:
        """同空间模型切换：落盘时间线分隔线；web 若正在看该空间，立刻推 runtime。"""
        payload = getattr(event, "payload", None) or {}
        try:
            view = self._view_coara()
        except Exception:
            return
        event_sid = str(payload.get("session_id") or "").strip()
        view_sid = str(getattr(view, "session_id", "") or "")
        event_ws = str(payload.get("workspace_dir") or "").strip()
        view_ws = str(getattr(view, "workspace_dir", "") or "")
        event_wid = str(payload.get("workspace_id") or "").strip()
        matched = False
        if event_sid and event_sid == view_sid:
            matched = True
        elif event_ws and view_ws:
            try:
                matched = Path(event_ws).resolve() == Path(view_ws).resolve()
            except Exception:
                matched = False
        if not matched and event_wid:
            pinned = getattr(self.root, "pinned_view_id", None)
            view_id = pinned("web") if callable(pinned) else None
            matched = bool(view_id) and str(view_id) == event_wid
        if not matched:
            return
        provider = str(payload.get("provider") or payload.get("provider_name") or "").strip()
        model = str(payload.get("model") or payload.get("model_name") or "").strip()
        # 显示必落带：他端（CLI/手机）切模型的时间线分隔线随事件落盘进权威
        # 视图——此前只有实时分隔线（liveTail 残留的最大来源），hydrate 权威
        # 历史里没有它，刷新后 reconcile 才能把它按同 key 原位接管而非追加。
        origin = str(payload.get("origin_source") or payload.get("source") or "").strip()
        if origin != "web":
            key = f"{provider}·{model}" if provider and model else (model or provider)
            if key:
                self._persist_timeline_divider(f"已切换模型 {key}", subject="root")
        if not self.registry.has_active():
            return
        # 视图 coara 可能尚未 apply deferred 内存切换：以事件为准刷 chrome
        runtime = self._build_lightweight_runtime()
        if runtime is None:
            return
        if provider:
            runtime["provider"] = provider
        if model:
            runtime["model"] = model
        self._spawn_bg_task(self.registry.send_to_active({"type": "state", "data": {"runtime": runtime}}))

    def _on_registry_changed_event(self, event: Any) -> None:
        """Push a workspace-list refresh when the registry changes (add/remove/rename).

        The browser caches its workspace list; without this push it would show
        ghost entries after a CLI ``coara ws remove`` or ws-tool change.
        (Subscribed by exact topic — no event_type re-check needed.)
        """
        if not self.registry.has_active():
            return
        payload = getattr(event, "payload", None) or {}
        self._spawn_bg_task(
            self.registry.send_to_active(
                {
                    "type": "workspaces_changed",
                    "action": str(payload.get("action") or ""),
                    "workspace_name": str(payload.get("workspace_name") or ""),
                }
            )
        )

    def _refresh_trace_store(self) -> None:
        """Point REST handlers at the web 视图 workspace's TraceStore（D6 按视图）。

        With :class:`~src.ui.trace_recording.MultiWorkspaceTracePersistence`,
        non-foreground stores stay open so a departing turn can keep writing.
        web 视图独立后用 store_for（只换查询指针，不动全局前台 _fg_key）。
        """
        view_dir = self._view_coara().workspace_dir
        router = getattr(self.root, "trace_persistence", None)
        if router is not None:
            # 跟随前台（未独立绑定）时视图=前台，set_foreground 与 store_for 等价；
            # 独立绑定后必须用 store_for——不能用 set_foreground（会改全局 _fg_key）。
            pinned = getattr(self.root, "pinned_view_id", None)
            if callable(pinned) and pinned("web"):
                self.trace_store = router.store_for(view_dir)
            else:
                self.trace_store = router.set_foreground(view_dir)
        else:
            # Web-only / tests without a shared router: recreate the single store.
            if self._trace_persistence_sub is not None:
                self._trace_persistence_sub.unsubscribe()
                if self._trace_persistence_sub in self._subscriptions:
                    self._subscriptions.remove(self._trace_persistence_sub)
                self._trace_persistence_sub = None
            try:
                self.trace_store.flush(timeout=1.0)
                self.trace_store.close()
            except Exception as exc:
                logger.debug(f"Trace store flush/close during workspace refresh failed: {exc}")
            self.trace_store = TraceStore(
                view_dir,
                coara_home=self.coara_home,
            )
            if not self.skip_trace_persistence:
                sub = subscribe_trace_persistence(self.root.event_bus, self.trace_store)
                self._trace_persistence_sub = sub
                self._subscriptions.append(sub)

        if hasattr(self, "_dash_ref") and self._dash_ref is not None:
            self._dash_ref.store = self.trace_store
            self._dash_ref.workspace_dir = view_dir
            self._dash_ref.reset_state_cache()
        self._last_runtime_snapshot = None
        # 视图存储跟随视图空间：后续回合帧落到新空间的 web_views 文件。
        # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_view_store 可能不存在
        if getattr(self, "_view_store", None) is not None:
            self._bind_view_store(view_dir)

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
from pathlib import Path
from time import time
from typing import Any

try:
    from nio import (
        InviteMemberEvent,
        LoginResponse,
        MatrixRoom,
        RoomMessageFile,
        RoomMessageImage,
        RoomMessageText,
        RoomMessageUnknown,
    )
except ImportError as e:
    raise ImportError(
        "matrix-nio is required. Install it in the coara environment:\n"
        "  pip install 'matrix-nio>=0.25.2' 'aiohttp>=3.9.0'\n"
    ) from e

from src.coara.root import create_root_coara
from src.core.config import config_manager
from src.core.logger import logger
from src.llm.registry import initialize_providers
from src.matrix_client.client_bootstrap import (
    create_matrix_client,
    fetch_matrix_agents,
    join_pending_invite_rooms,
    login_and_prepare_sync_token,
    route_inbound_text_event,
    run_matrix_client_sync_loop,
    warn_if_bot_missing_from_agents,
    wire_matrix_file_bridge,
    wire_matrix_notify_pipeline,
)
from src.matrix_client.mention_routing import (
    AgentDescriptor,
    localpart_from_user_id,
)
from src.matrix_client.sync_helpers import BUSY_DROP_NOTICE, MatrixMessageDispatcher


class CoaraMatrixBot:
    """coara as a native Matrix client — no middleman."""

    def __init__(
        self,
        homeserver: str = "http://localhost:8008",
        user_id: str = "@coara:coara.local",
        password: str | None = None,
        device_id: str = "COARA_AGENT",
        device_name: str = "coara Agent",
        server_name: str = "coara.local",
    ):
        self.homeserver = homeserver
        self.user_id = user_id
        self.password = password or os.getenv("COARA_MATRIX_PASSWORD") or os.getenv("MATRIX_PASSWORD", "")
        if not self.password:
            raise ValueError("COARA_MATRIX_PASSWORD must be configured for the Matrix bot")
        self.device_id = device_id
        self.device_name = device_name
        self.server_name = server_name
        self.token_path = Path.home() / ".coara" / "matrix_sync_token_agent"
        self._dispatcher = MatrixMessageDispatcher()
        self._known_agents: list[AgentDescriptor] = []
        self._bot_localpart = localpart_from_user_id(user_id)
        # 图片批量聚合的回调任务集合（防 GC 回收协程，完成即丢弃）
        self._media_batch_tasks: set[asyncio.Task[None]] = set()

        self.client = create_matrix_client(
            homeserver=homeserver,
            user_id=user_id,
            device_id=device_id,
        )
        self.root = None
        self._started_at: float = 0.0
        self._coara_home: Path | str | None = None

    async def setup(self) -> None:
        """Initialize coara and log in to Matrix."""
        await config_manager.load()
        await initialize_providers(config_manager)

        coara_home = config_manager.config.coara_home
        self._coara_home = coara_home
        from src.coara.workspace_runtime import resolve_matrix_workspace_path

        fallback = Path(__file__).parent.parent.parent
        workspace, active_runtime = resolve_matrix_workspace_path(
            coara_home=coara_home,
            fallback_workspace=fallback,
        )
        if active_runtime is not None:
            logger.info(f"[Matrix] Bound to active CLI workspace {active_runtime.workspace_name} ({workspace})")
        else:
            logger.warning(
                f"[Matrix] No live local coara CLI — using fallback workspace {workspace}. "
                "Start `coara -dx` in your workspace directory so the remote client uses the same workspace."
            )
        self.root = await create_root_coara(
            workspace_dir=workspace,
            provider_name=config_manager.config.default_provider or None,
            model=config_manager.config.default_model or None,
        )
        logger.info(f"coara initialized: {self.root.identity.name}")

        from src.matrix_client.ingress_helpers import matrix_join_room_with_retry

        async def _ensure_joined(room_id: str) -> bool:
            ok, _err = await matrix_join_room_with_retry(
                homeserver=self.homeserver,
                access_token=self.client.access_token,
                room_id=room_id,
            )
            return ok

        self._file_bridge = wire_matrix_file_bridge(
            root=self.root,
            client=self.client,
            homeserver=self.homeserver,
            workspace_root=workspace,
            ensure_joined=_ensure_joined,
        )
        logger.info("[Matrix] Remote file tool enabled: send_file")

        # Discover other agents from the homeserver.
        await self._discover_agents()

        resp = await login_and_prepare_sync_token(
            self.client,
            password=self.password,
            device_name=self.device_name,
            token_path=self.token_path,
            label="Matrix Agent",
        )
        if isinstance(resp, LoginResponse):
            logger.info(f"[✓] Logged in as {self.user_id}")
            logger.info(f"[✓] Access token acquired (len={len(resp.access_token)})")
        else:
            raise RuntimeError(f"[✗] Matrix login failed: {resp}")

        from src.matrix_client.ingress_helpers import matrix_raw_join

        matrix_cfg = config_manager.config.matrix
        notify_room_id = (matrix_cfg.notify_room_id if matrix_cfg else "") or ""
        wire_matrix_notify_pipeline(
            root=self.root,
            client=self.client,
            notify_room_id=notify_room_id,
            coara_home=coara_home,
        )
        if notify_room_id:
            joined = await matrix_raw_join(
                homeserver=self.homeserver,
                access_token=self.client.access_token,
                room_id=notify_room_id,
            )
            if joined:
                logger.info(f"[+] Joined notify room {notify_room_id}")
            else:
                logger.warning(f"[!] Failed to join notify room {notify_room_id}")

        from src.coara.mobile_sync import push_mobile_sync_payloads

        push_mobile_sync_payloads(self.root, notify_room_id)

        # Subscribe to workspace_registry_changed so registry edits from Web/CLI
        # are pushed to the mobile client.
        # workspace_switched 只在 cli view 切换时发布（payload.end 恒为 cli），
        # matrix 自身变更走 view_changed / 命令路径内推送——不订 workspace_switched。
        from src.coara.mobile_sync import (
            event_matches_matrix_view,
            push_model_switch_payloads,
            push_status_for_session_started,
            push_workspaces_payload,
        )

        def _on_model_switched(event: Any) -> None:
            payload = getattr(event, "payload", None) or {}
            if not event_matches_matrix_view(self.root, payload):
                return
            provider = str(payload.get("provider") or "").strip()
            model = str(payload.get("model") or "").strip()
            label = f"{provider}·{model}" if provider else model
            push_model_switch_payloads(
                self.root,
                fallback_room_id=notify_room_id,
                model_label=label,
            )

        self._model_switch_sub = self.root.event_bus.subscribe(
            callback=_on_model_switched,
            topic="model_switched",
        )

        def _on_session_started(event: Any) -> None:
            push_status_for_session_started(self.root, event, fallback_room_id=notify_room_id)

        self._session_started_sub = self.root.event_bus.subscribe(
            callback=_on_session_started,
            topic="session_started",
        )

        def _on_registry_changed(event: Any) -> None:
            # Registry add/remove/rename: refresh the workspace list on the phone.
            push_workspaces_payload(self.root, fallback_room_id=notify_room_id)

        self._registry_changed_sub = self.root.event_bus.subscribe(
            callback=_on_registry_changed,
            topic="workspace_registry_changed",
        )

        self._started_at = time()
        self._ingress_host = self._make_ingress_host()

        async def _send_matrix_text(room_id: str, body: str) -> None:
            await self._send_response(room_id, body)

        from src.matrix_client.diff_bridge import wire_matrix_diff_send_text

        # 常驻发送回调登记（push_matrix_text 与 diff 兜底房间解析用）；
        # diff 已统一走 EndRegistry 段路由（matrix sender 直接发房间）。
        wire_matrix_diff_send_text(_send_matrix_text, self.root)

    def _make_ingress_host(self):
        from src.matrix_client.inbound_handlers import MatrixInboundHost

        async def send_text(room_id: str, body: str) -> bool:
            return await self._send_response(room_id, body)

        async def report_text_error(room_id: str, _exc: Exception | None) -> None:
            from src.matrix_client.send_guard import matrix_room_send_text

            await matrix_room_send_text(self.client, room_id, "✗ Error: message processing failed.")

        async def report_media_error(room_id: str, exc: Exception | None) -> None:
            await self._send_response(room_id, f"✗ 处理文件时出错: {exc}")

        async def on_inbound_text(room: MatrixRoom, event: RoomMessageText) -> None:
            logger.info(f"[<-] {room.user_name(event.sender)}: {event.body[:80]}")

        return MatrixInboundHost(
            root=self.root,
            client=self.client,
            file_bridge=self._file_bridge,
            coara_home=self._coara_home,
            cli_owner=False,
            send_chunk=self._send_response,
            send_text=send_text,
            send_room_text=self._send_response,
            report_text_error=report_text_error,
            report_media_error=report_media_error,
            on_inbound_text=on_inbound_text,
            on_untrusted_sender=lambda sender: logger.info(f"[Security] Untrusted Matrix message from {sender}"),
        )

    async def _send_response(self, room_id: str, response: str) -> bool:
        from src.matrix_client.send_guard import matrix_room_send_text

        ok = await matrix_room_send_text(self.client, room_id, response)
        if ok:
            logger.debug(f"[->] Replied to {room_id}")
        else:
            logger.warning(f"[!] Failed to send reply to {room_id}")
        return ok

    async def _discover_agents(self) -> None:
        """Query the homeserver's /api/agents endpoint to discover other agents."""

        def _on_error(exc: Exception) -> None:
            logger.warning(f"[Matrix] Agent discovery failed: {exc}")
            # Fallback: assume we are the default agent.
            self._known_agents = [
                AgentDescriptor(
                    name=self._bot_localpart,
                    user_id=self.user_id,
                    display_name="coara",
                    is_default=True,
                )
            ]

        agents_data = await fetch_matrix_agents(
            self.homeserver,
            on_http_error=lambda status: logger.warning(f"[Matrix] /api/agents returned {status}"),
            on_error=_on_error,
        )
        if agents_data is None:
            return
        try:
            self._known_agents = [
                AgentDescriptor(
                    name=a.get("name", ""),
                    user_id=a.get("user_id", ""),
                    display_name=a.get("display_name", ""),
                    is_default=a.get("default", False),
                )
                for a in agents_data
            ]
        except Exception as exc:
            _on_error(exc)
            return
        agent_names = [a.name for a in self._known_agents]
        logger.info(f"[Matrix] 在线智能体: {agent_names}")
        warn_if_bot_missing_from_agents(bot_localpart=self._bot_localpart, agents=self._known_agents)

    async def _process_text_message(
        self,
        room: MatrixRoom,
        event: RoomMessageText,
        *,
        bind_coara: Any | None = None,
        bind_ws_id: str | None = None,
    ) -> None:
        from src.matrix_client.inbound_handlers import process_matrix_text_message

        await process_matrix_text_message(self._ingress_host, room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id)

    async def _process_media_message(
        self,
        room: MatrixRoom,
        event: RoomMessageFile | RoomMessageImage,
        *,
        bind_coara: Any | None = None,
        bind_ws_id: str | None = None,
    ) -> None:
        from src.matrix_client.inbound_handlers import process_matrix_media_message

        await process_matrix_media_message(
            self._ingress_host, room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id
        )

    async def on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Dispatch only — must not block the /sync long-poll."""
        from src.matrix_client.ingress_helpers import (
            prefilter_matrix_text_event,
            try_defer_to_continuation_input,
        )

        route = route_inbound_text_event(
            sender=event.sender,
            bot_user_id=self.user_id,
            body=event.body,
            bot_localpart=self._bot_localpart,
            known_agents=self._known_agents,
        )
        if route is None:
            return

        # If mention was stripped, update the event body.
        # NOTE: intentional drift from matrix_runner.py — deepcopy + re-parse
        # (the runner uses a dict spread). See client_bootstrap module docstring.
        if route.body != event.body:
            event = type(event)(
                source=event.source,
                sender=event.sender,
                server_timestamp=event.server_timestamp,
                decrypted=event.decrypted,
                verified=event.verified,
            )
            # nio Event objects are immutable; patch the source dict and re-parse.
            patched_source = copy.deepcopy(event.source)
            patched_source.setdefault("content", {})["body"] = route.body
            event = RoomMessageText.from_dict(
                patched_source,
                room.room_id,
                event.sender,
                event.server_timestamp,
            )

        if prefilter_matrix_text_event(
            root=self.root,
            room_id=room.room_id,
            body=event.body,
            client=self.client,
            send_text=self._send_response,
        ):
            return
        # 保序：文本到达前先把该房间聚合中的图片批次冲刷投递，图永远排在文本前
        from src.matrix_client.media_batch import media_batch_aggregator

        media_batch_aggregator.flush_now(room.room_id)
        from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL

        if try_defer_to_continuation_input(
            self.root,
            event.body,
            channel="matrix",
            room_id=room.room_id,
            send_text=self._send_response,
            interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
            actor=str(event.sender or ""),
        ):
            return

        from src.matrix_client.chat_commands import is_ws_command, try_handle_matrix_chat_command

        # /ws must not wait on the busy-turn dispatcher lock (leave A running).
        if is_ws_command(event.body):
            self._dispatcher.schedule_unlocked(
                try_handle_matrix_chat_command(
                    self.root,
                    event.body,
                    send_text=lambda body: self._send_response(room.room_id, body),
                    room_id=room.room_id,
                ),
                label="ws-command",
            )
            return

        from src.matrix_client.ingress_helpers import capture_matrix_turn_binding, matrix_view_session_key

        # matrix 独立视图（D6）：按 matrix 自己的视图空间串行，不随全局前台漂移。
        fg_key = matrix_view_session_key(self.root)
        bind_coara, bind_ws_id = capture_matrix_turn_binding(self.root)
        self._dispatcher.schedule(
            self._process_text_message(room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id),
            label="agent-text",
            session_key=fg_key,
            on_busy=lambda: self._send_response(
                room.room_id,
                BUSY_DROP_NOTICE,
            ),
        )

    async def on_file(self, room: MatrixRoom, event: RoomMessageFile) -> None:
        from src.matrix_client.ingress_helpers import (
            capture_matrix_turn_binding,
            matrix_view_session_key,
            should_skip_matrix_self_event,
        )

        if should_skip_matrix_self_event(sender=event.sender, bot_user_id=self.user_id):
            return
        # matrix 独立视图（D6）：按 matrix 自己的视图空间串行，不随全局前台漂移。
        fg_key = matrix_view_session_key(self.root)
        bind_coara, bind_ws_id = capture_matrix_turn_binding(self.root)
        self._dispatcher.schedule(
            self._process_media_message(room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id),
            label="agent-file",
            session_key=fg_key,
            on_busy=lambda: self._send_response(
                room.room_id,
                BUSY_DROP_NOTICE,
            ),
        )

    async def on_unknown_room_message(self, room: MatrixRoom, event: RoomMessageUnknown) -> None:
        """自定义 msgtype 的 m.room.message（nio RoomMessageUnknown）。

        审批回执专用入口：m.coara.approval_reply 直接进 ApprovalCenter.resolve
        （在调度前执行——持锁等待的审批回合靠它释放），其余未知 msgtype 忽略。
        """
        from src.matrix_client.approval_bridge import handle_approval_reply_event

        content = getattr(event, "content", None)
        if not isinstance(content, dict):
            source = getattr(event, "source", None)
            if isinstance(source, dict):
                content = source.get("content")
        handle_approval_reply_event(
            sender=event.sender,
            bot_user_id=self.user_id,
            content=content,
        )

    async def on_image(self, room: MatrixRoom, event: RoomMessageImage) -> None:
        """图片消息：回调入口直接下载组块进批量聚合器，不占调度锁、免 BUSY_DROP。

        聚合窗口届满（或文本到达冲刷）时整批经调度锁投递开回合。
        """
        from src.matrix_client.ingress_helpers import (
            capture_matrix_turn_binding,
            guest_room_allowed,
            matrix_view_session_key,
            resolve_matrix_trust_level,
            should_skip_matrix_self_event,
        )
        from src.matrix_client.media_inbound import build_media_batch_handler

        if should_skip_matrix_self_event(sender=event.sender, bot_user_id=self.user_id):
            return
        trust_level = resolve_matrix_trust_level(event.sender, cli_owner=self._ingress_host.cli_owner)
        if trust_level == "untrusted" and not guest_room_allowed(room.room_id):
            return

        def _deliver_batch(room_id: str, blocks: list, caption: str) -> None:
            fg_key = matrix_view_session_key(self.root)
            bind_coara, bind_ws_id = capture_matrix_turn_binding(self.root)
            self._dispatcher.schedule(
                self._deliver_media_batch(room_id, blocks, caption, bind_coara=bind_coara, bind_ws_id=bind_ws_id),
                label="agent-image-batch",
                session_key=fg_key,
                on_busy=lambda: self._send_response(room_id, BUSY_DROP_NOTICE),
            )

        handler = build_media_batch_handler(
            self.client,
            self.root,
            trust_level=trust_level,
            send_room_text=lambda rid, body: self._send_response(rid, body),
            deliver_batch=_deliver_batch,
        )
        task = asyncio.create_task(handler(room, event))
        self._media_batch_tasks.add(task)
        task.add_done_callback(self._media_batch_tasks.discard)

    async def _deliver_media_batch(
        self,
        room_id: str,
        blocks: list,
        caption: str,
        *,
        bind_coara: Any | None = None,
        bind_ws_id: str | None = None,
    ) -> None:
        from src.coara.background_activity import background_work_active
        from src.matrix_client.media_inbound import _deliver_image_turn
        from src.matrix_client.turn_signal import matrix_turn_scope, turn_send_stats

        # 正文 chunk 失败计数：批次正文走 stats.send，结束信封带 chunk_lost 标记
        raw_send_chunk = self._ingress_host.send_chunk
        turn_stats = turn_send_stats(raw_send_chunk)
        turn_send = turn_stats.send if turn_stats is not None else raw_send_chunk
        async with matrix_turn_scope(
            room_id,
            send_chunk=raw_send_chunk,
            background_active=background_work_active,
            send_stats=turn_stats,
        ):
            await _deliver_image_turn(
                self.root,
                room_id,
                caption,
                blocks,
                trust_level="owner",
                send_chunk=turn_send,
                echo_tool_summary_local=self._ingress_host.echo_tool_summary_local,
                bind_coara=bind_coara,
                bind_ws_id=bind_ws_id,
            )
            # 与既有媒体/文本路径一致：批次回合收尾后遗留的接续输入立即排空
            turn_coara = bind_coara if bind_coara is not None else self.root.foreground_coara
            if turn_coara is not None:
                from src.matrix_client.inbound_handlers import _drain_leftover_continuations

                room = self.client.rooms.get(room_id)
                if room is not None:
                    await _drain_leftover_continuations(
                        self._ingress_host,
                        room,
                        turn_coara=turn_coara,
                        turn_ws_id=bind_ws_id,
                        trust_level="owner",
                    )

    async def _raw_join(self, room_id: str) -> bool:
        from src.matrix_client.ingress_helpers import matrix_raw_join

        joined = await matrix_raw_join(
            homeserver=self.homeserver,
            access_token=self.client.access_token,
            room_id=room_id,
        )
        if joined:
            logger.info(f"[+] Joined {room_id}")
        else:
            logger.error(f"[!] Join {room_id} failed")
        return joined

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        from src.matrix_client.ingress_helpers import should_accept_matrix_invite

        if should_accept_matrix_invite(sender=event.sender, bot_user_id=self.user_id):
            logger.info(f"[+] Accepting invite to {room.room_id} from {event.sender}")
            await self._raw_join(room.room_id)

    async def _join_pending_invites(self) -> None:
        async def _join(room_id: str) -> None:
            logger.info(f"[+] Joining pending invite room {room_id}")
            await self._raw_join(room_id)

        await join_pending_invite_rooms(self.client, _join, label="Matrix Agent")

    async def run(self) -> None:
        await self.setup()
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self.on_message, RoomMessageText)
        self.client.add_event_callback(self.on_file, RoomMessageFile)
        self.client.add_event_callback(self.on_image, RoomMessageImage)
        self.client.add_event_callback(self.on_unknown_room_message, RoomMessageUnknown)

        # 审批走自定义 msgtype：登记房间事件发送通道（进程内单 client，覆盖式幂等）
        from src.matrix_client.approval_bridge import register_approval_room_sender
        from src.matrix_client.send_guard import matrix_room_send_content

        async def _send_room_content(room_id: str, content: dict) -> bool:
            return await matrix_room_send_content(self.client, room_id, content)

        register_approval_room_sender(_send_room_content)

        await self._join_pending_invites()

        # Restart invalidation sweep: vault unlock cards sent by a previous
        # process are dead (approval futures live in ApprovalCenter and settle
        # via m.coara.approval_resolved) — notify the room once and clear the record.
        from src.matrix_client.pending_interactions import notify_stale_pending_interactions

        try:
            _stale_notified = await notify_stale_pending_interactions(self._send_response)
            if _stale_notified:
                logger.info(f"[Matrix] Notified {_stale_notified} stale pending interaction(s)")
        except Exception as exc:
            logger.warning(f"[Matrix] stale pending-interaction sweep failed: {exc}")

        logger.info("[✓] coara Matrix Bot is running. Press Ctrl+C to stop.")
        logger.info(f"[✓] Homeserver: {self.homeserver}")
        logger.info(f"[✓] User: {self.user_id}")

        # matrix 独立视图：启动时 lifecycle 已 pin 三端；此处仅兜底未 pin 的替身/早起路径。
        try:
            pinned = getattr(self.root, "pinned_view_id", None)
            if callable(pinned) and not pinned("matrix"):
                fg = str(getattr(self.root, "_foreground_session_id", None) or "")
                if fg:
                    self.root._matrix_view_workspace_id = fg
        except Exception as exc:
            logger.debug(f"[Matrix] init matrix view binding skipped: {exc}")

        try:
            await run_matrix_client_sync_loop(
                self.client,
                on_sync_error=lambda err: logger.warning(f"[!] Sync error: {err!r}"),
                on_link_change=lambda up: logger.warning(
                    "[Matrix] homeserver link %s", "restored" if up else "DOWN (probe failing)"
                ),
                token_path=self.token_path,
                label="Matrix Agent",
                dispatcher=self._dispatcher,
            )
        finally:
            # Durable next_batch advances only after each sync generation's handlers
            # apply (see MatrixMessageDispatcher.schedule_token_persist); drain below
            # lets in-flight persist tasks finish before logout.
            logger.info("[✓] Shutting down...")
            if hasattr(self, "_model_switch_sub") and self._model_switch_sub is not None:
                self._model_switch_sub.unsubscribe()
            if hasattr(self, "_session_started_sub") and self._session_started_sub is not None:
                self._session_started_sub.unsubscribe()
            if hasattr(self, "_registry_changed_sub") and self._registry_changed_sub is not None:
                self._registry_changed_sub.unsubscribe()
            # 先 drain 在跑的消息 handler（含其内发起的回合），避免 logout/close
            # 后 handler 还在向已关闭的 client 发响应
            with contextlib.suppress(Exception):
                await self._dispatcher.drain()
            await self.root.shutdown()
            await self.client.logout()
            await self.client.close()
            # 文件桥共享会话随组件退出统一关闭
            if hasattr(self, "_file_bridge") and self._file_bridge is not None:
                with contextlib.suppress(Exception):
                    await self._file_bridge.aclose()
            logger.info("[✓] Bot stopped.")

"""Matrix remote CLI background runner."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.shortcuts import print_formatted_text
from rich.console import Console
from rich.markup import escape

from src.cli.product_defaults import MATRIX_BOT_USER
from src.core.logger import logger

console = Console()


def _echo_tool_summary_local(chunk: str) -> None:
    """把手机回合的工具摘要行（✓/✗）回显到本机终端。

    内核常为 win32 detach 的无头 daemon：stdout/stderr 重定向到日志文件、
    非 tty，Rich 探测不到终端能力时回退到 Windows 传统代码页 GBK(cp936)。
    GBK 编不了 ✓(\\u2713) 会抛 UnicodeEncodeError，冒泡后整个手机回合被判失败、
    错误正文被 report_text_error 发到手机——这就是手机偶发
    「✗ Error: 'gbk' codec can't encode ✓」的根因。

    双保险：无 tty 时直接不回显（无头内核没人看终端）；有 tty 时也兜 try/except，
    本地回显失败绝不影响手机回合。
    """
    if not sys.stdout.isatty():
        return
    try:
        console.print(f"[dim]{escape(chunk)}[/dim]")
    except Exception:  # noqa: BLE001 — 本地回显失败不阻塞手机回合
        logger.debug("[Matrix] tool-summary local echo failed", exc_info=True)


async def run_matrix_client(
    root,
    config: dict,
    *,
    coara_home: Path | None = None,
    matrix_dispatcher_holder: list[Any] | None = None,
):
    """Run Matrix client in background (remote CLI and/or webhook event push to phone)."""
    try:
        await _run_matrix_client_inner(
            root,
            config,
            coara_home=coara_home,
            matrix_dispatcher_holder=matrix_dispatcher_holder,
        )
    except Exception as exc:
        logger.exception("[Matrix] background client crashed: {}", exc)
        console.print(f"[red][Matrix] 连接异常已退出（不影响本机聊天）: {exc}[/red]")


async def _run_matrix_client_inner(
    root,
    config: dict,
    *,
    coara_home: Path | None = None,
    matrix_dispatcher_holder: list[Any] | None = None,
):
    """Inner Matrix client loop (exceptions handled by ``run_matrix_client``)."""
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
    except ImportError:
        console.print("[yellow][Matrix] matrix-nio not installed. Remote CLI disabled.[/yellow]")
        console.print("[dim]Run: pip install 'matrix-nio>=0.25.2' 'aiohttp>=3.9.0'[/dim]")
        return

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
    from src.matrix_client.ingress_helpers import matrix_join_room_with_retry
    from src.matrix_client.mention_routing import AgentDescriptor
    from src.matrix_client.send_guard import matrix_room_send_text

    token_path = Path.home() / ".coara" / "matrix_sync_token_cli"

    client = create_matrix_client(
        homeserver=config["homeserver"],
        user_id=config["user"],
        device_id="COARA_CLI",
    )

    async def _close_client() -> None:
        with contextlib.suppress(Exception):
            await client.close()

    try:
        resp = await login_and_prepare_sync_token(
            client,
            password=config["password"],
            device_name="coara CLI",
            token_path=token_path,
            label="Matrix CLI",
            on_parked=lambda parked: console.print(
                f"[dim][Matrix] Sync token parked at {parked} (skipped historic timeline)[/dim]"
            ),
        )
    except Exception as exc:
        console.print(f"[red][Matrix] Login error: {exc}[/red]")
        await _close_client()
        return

    if not isinstance(resp, LoginResponse):
        console.print(f"[red][Matrix] Login failed: {resp}[/red]")
        agents_hint = ""
        if coara_home is not None:
            from src.matrix_host.credentials import load_matrix_password_from_data_dir

            if not load_matrix_password_from_data_dir(coara_home):
                agents_hint = "（gomatrix.toml 缺少 [[agents]] coara，重启 coara 会自动修复）"
        console.print(
            "[dim]检查 GoMatrix 是否在运行，以及 system\\.env 中 "
            "COARA_MATRIX_USER / COARA_MATRIX_PASSWORD 是否与 "
            f"COARA_HOME\\matrix\\gomatrix.toml [[agents]] 一致{agents_hint}。"
            f"默认用户：{MATRIX_BOT_USER}[/dim]"
        )
        await _close_client()
        return

    notify_room_id = str(config.get("notify_room_id") or "").strip()

    bot_user_id = str(config["user"])
    bot_localpart = bot_user_id.removeprefix("@").split(":", 1)[0]
    known_agents: list[AgentDescriptor] = [
        AgentDescriptor(name=bot_localpart, user_id=bot_user_id, display_name="coara", is_default=True),
    ]
    _join_locks: dict[str, asyncio.Lock] = {}

    async def _join_room(room_id: str, *, label: str = "room") -> bool:
        lock = _join_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            ok, err = await matrix_join_room_with_retry(
                homeserver=config["homeserver"],
                access_token=client.access_token,
                room_id=room_id,
            )
            if ok:
                logger.debug("[Matrix] Joined {} {}", label, room_id)
            else:
                logger.warning("[Matrix] Join failed ({}) {}: {}", label, room_id, err or "unknown")
            return ok

    # NOTE: intentional drift from bot.py — the CLI runner guards message
    # dispatch with join retries; the bot dispatches without a join guard.
    # See client_bootstrap module docstring.
    async def _ensure_joined(room_id: str, *, attempts: int = 5) -> bool:
        for attempt in range(attempts):
            if await _join_room(room_id, label="room"):
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(0.12 * (2**attempt))
        return False

    file_bridge = wire_matrix_file_bridge(
        root=root,
        client=client,
        homeserver=config["homeserver"],
        workspace_root=Path(root.foreground_coara.workspace_dir),
        ensure_joined=_ensure_joined,
    )

    wire_matrix_notify_pipeline(
        root=root,
        client=client,
        notify_room_id=notify_room_id,
        coara_home=coara_home,
    )

    # 常驻发送回调登记：diff 已统一走 EndRegistry 段路由（matrix sender 直接
    # 发房间），此处仅登记常驻回调供 push_matrix_text（子智能体最终结果等
    # 独立文本推送）与兜底房间解析使用。
    from src.matrix_client.diff_bridge import wire_matrix_diff_send_text

    async def _send_matrix_text(room_id: str, body: str) -> None:
        await matrix_room_send_text(client, room_id, body)

    wire_matrix_diff_send_text(_send_matrix_text, root)

    from src.coara.mobile_sync import push_mobile_sync_payloads

    push_mobile_sync_payloads(root, notify_room_id)

    # workspace_switched 只在 cli view 切换时发布；matrix 变更走命令路径内推送。
    from src.coara.mobile_sync import (
        event_matches_matrix_view,
        push_model_switch_payloads,
        push_status_for_session_started,
        push_workspaces_payload,
    )

    def _on_model_switched(event: Any) -> None:
        payload = getattr(event, "payload", None) or {}
        if not event_matches_matrix_view(root, payload):
            return
        provider = str(payload.get("provider") or "").strip()
        model = str(payload.get("model") or "").strip()
        label = f"{provider}·{model}" if provider else model
        push_model_switch_payloads(
            root,
            fallback_room_id=notify_room_id,
            model_label=label,
        )

    model_switch_sub = root.event_bus.subscribe(
        callback=_on_model_switched,
        topic="model_switched",
    )

    def _on_session_started(event: Any) -> None:
        push_status_for_session_started(root, event, fallback_room_id=notify_room_id)

    session_started_sub = root.event_bus.subscribe(
        callback=_on_session_started,
        topic="session_started",
    )

    def _on_registry_changed(event: Any) -> None:
        # Registry add/remove/rename (in-process or via CLI reload): refresh the
        # phone's workspace list.
        push_workspaces_payload(root, fallback_room_id=notify_room_id)

    registry_changed_sub = root.event_bus.subscribe(
        callback=_on_registry_changed,
        topic="workspace_registry_changed",
    )

    if notify_room_id:
        joined = await _join_room(notify_room_id, label="notify")
        if not joined:
            root.matrix_notify.clear_persisted_room(coara_home)

    async def _discover_agents() -> None:
        nonlocal known_agents
        agents_data = await fetch_matrix_agents(
            config["homeserver"],
            on_error=lambda exc: logger.debug(f"[Matrix] Agent discovery failed: {exc}"),
        )
        if agents_data is None:
            return
        try:
            discovered = [
                AgentDescriptor(
                    name=str(a.get("name", "")),
                    user_id=str(a.get("user_id", "")),
                    display_name=str(a.get("display_name", "")),
                    is_default=bool(a.get("default", False)),
                )
                for a in agents_data
                if a.get("name")
            ]
        except Exception as exc:
            logger.debug(f"[Matrix] Agent discovery failed: {exc}")
            return
        if discovered:
            # NOTE: intentional drift from bot.py — when the server lists no
            # default agent, promote the descriptor matching our localpart.
            if not any(a.is_default for a in discovered):
                discovered = [
                    AgentDescriptor(
                        name=a.name,
                        user_id=a.user_id,
                        display_name=a.display_name,
                        is_default=a.name.lower() == bot_localpart.lower(),
                    )
                    for a in discovered
                ]
            known_agents = discovered
            warn_if_bot_missing_from_agents(bot_localpart=bot_localpart, agents=known_agents)

    await _discover_agents()

    from src.matrix_client.inbound_handlers import (
        MatrixInboundHost,
        process_matrix_media_message,
        process_matrix_text_message,
    )
    from src.matrix_client.sync_helpers import BUSY_DROP_NOTICE, MatrixMessageDispatcher

    async def on_invite(room: MatrixRoom, event: InviteMemberEvent) -> None:
        from src.matrix_client.ingress_helpers import should_accept_matrix_invite

        if should_accept_matrix_invite(sender=event.sender, bot_user_id=bot_user_id):
            logger.debug("[Matrix] Accepting invite to %s from %s", room.room_id, event.sender)
            await _join_room(room.room_id, label="invite")

    async def _join_pending_invites() -> None:
        async def _join(room_id: str) -> None:
            logger.debug("[Matrix] Joining pending invite room %s", room_id)
            await _join_room(room_id, label="pending-invite")

        await join_pending_invite_rooms(
            client,
            _join,
            label="Matrix CLI",
            on_error=lambda exc: logger.warning(f"[Matrix] Initial invite check error: {exc}"),
        )

    async def _handle_matrix_response(room_id: str, response: str) -> bool:
        ok = await matrix_room_send_text(
            client,
            room_id,
            response,
            ensure_joined=lambda: _join_room(room_id, label="reply"),
        )
        if not ok:
            logger.warning("[Matrix] reply not delivered to {} (chunk len={})", room_id, len(response))
        return ok

    async def _on_inbound_media(room: MatrixRoom, event: RoomMessageFile | RoomMessageImage) -> None:
        # 本地终端预览纯属回显：无头 daemon（tray / 重定向 stdout）下
        # prompt_toolkit 拿不到屏幕缓冲会抛 NoConsoleScreenBufferError，把整个
        # 图片后台任务打断（手机图进不了 coara，文字却正常——图片专线断点）。
        # 与 _echo_tool_summary_local 同策略：无控制台降级为日志，回显失败
        # 绝不影响媒体入站。
        try:
            print_formatted_text(HTML(f"\n<bold><cyan>[Matrix] {event.sender}:</cyan></bold> <dim>(image/file)</dim>"))
        except Exception:  # noqa: BLE001 — 预览失败不阻塞媒体入站
            logger.info("[Matrix] inbound media from %s (image/file)", event.sender)

    async def _send_text(room_id: str, body: str) -> bool:
        return await matrix_room_send_text(
            client,
            room_id,
            body,
            ensure_joined=lambda: _join_room(room_id, label="reply"),
        )

    # 宿主回调契约要求 Awaitable[None]；本地实现带 bool 返回（投递成败），
    # 这里只做类型适配，调用方本就不使用返回值。
    async def _send_text_void(room_id: str, body: str) -> None:
        await _send_text(room_id, body)

    async def _handle_response_void(room_id: str, response: str) -> None:
        await _handle_matrix_response(room_id, response)

    async def _send_plain_text(room_id: str, body: str) -> None:
        await matrix_room_send_text(client, room_id, body)

    async def _maybe_send_turn_quiet() -> None:
        """后台级联工作收尾后：若已彻底安静（无后台工作、无活跃回合），
        给手机补发 [COARA_TURN] quiet 信封，收掉单点 typing 状态"""
        from src.coara.background_activity import background_work_active
        from src.matrix_client.turn_signal import MATRIX_TURN_QUIET_ENVELOPE

        if background_work_active():
            return
        if root.has_active_turn():
            return
        room_id = root.matrix_notify.last_remote_room_id or root.matrix_notify.default_room_id or notify_room_id
        if not room_id:
            return
        try:
            await _send_text(room_id, MATRIX_TURN_QUIET_ENVELOPE)
        except Exception as exc:
            logger.debug(f"[Matrix] turn quiet envelope send failed: {exc}")

    def _on_background_settled(event: Any) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(_maybe_send_turn_quiet())

    root.event_bus.subscribe(callback=_on_background_settled, topic="background_task_complete")
    root.event_bus.subscribe(callback=_on_background_settled, topic="background_agent_complete")

    async def _report_text_error(room_id: str, exc: Exception | None) -> None:
        # 不把原始异常回显到手机：技术细节（如 Windows GBK 编码失败）用户看不懂，
        # 完整堆栈已由 logger.exception 落日志。与 bot.py 的固定文案对齐。
        await matrix_room_send_text(client, room_id, "✗ Error: message processing failed.")

    async def _report_media_error(room_id: str, exc: Exception | None) -> None:
        await matrix_room_send_text(client, room_id, f"✗ 处理文件时出错: {exc}")

    ingress_host = MatrixInboundHost(
        root=root,
        client=client,
        file_bridge=file_bridge,
        coara_home=coara_home,
        cli_owner=True,
        send_chunk=_handle_response_void,
        send_text=_send_text_void,
        send_room_text=_send_text_void,
        report_text_error=_report_text_error,
        report_media_error=_report_media_error,
        echo_tool_summary_local=_echo_tool_summary_local,
        on_inbound_text=None,
        on_inbound_media=_on_inbound_media,
        text_error_log="[Matrix] Error processing remote message",
        media_error_log="[Matrix] Error processing media",
    )

    matrix_dispatcher = MatrixMessageDispatcher()
    if matrix_dispatcher_holder is not None:
        matrix_dispatcher_holder[0] = matrix_dispatcher

    async def _process_text_message(
        room: MatrixRoom,
        event: RoomMessageText,
        *,
        bind_coara: Any | None = None,
        bind_ws_id: str | None = None,
    ) -> None:
        await process_matrix_text_message(ingress_host, room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id)

    async def _process_media_message(
        room: MatrixRoom,
        event: RoomMessageFile | RoomMessageImage,
        *,
        bind_coara: Any | None = None,
        bind_ws_id: str | None = None,
    ) -> None:
        await process_matrix_media_message(ingress_host, room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id)

    async def on_message(room: MatrixRoom, event: RoomMessageText) -> None:
        from src.matrix_client.ingress_helpers import (
            prefilter_matrix_text_event,
            try_defer_to_continuation_input,
        )
        from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL

        route = route_inbound_text_event(
            sender=event.sender,
            bot_user_id=config["user"],
            body=event.body,
            bot_localpart=bot_localpart,
            known_agents=known_agents,
        )
        if route is None:
            return

        body = route.body
        if prefilter_matrix_text_event(
            root=root,
            room_id=room.room_id,
            body=body,
            client=client,
            send_text=_send_text,
        ):
            return
        # 保序：文本到达前先把该房间聚合中的图片批次冲刷投递，图永远排在文本前
        from src.matrix_client.media_batch import media_batch_aggregator

        media_batch_aggregator.flush_now(room.room_id)
        if try_defer_to_continuation_input(
            root,
            body,
            channel="matrix",
            room_id=room.room_id,
            send_text=_send_text,
            interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
            actor=str(getattr(event, "sender", "") or ""),
        ):
            return

        if not await _ensure_joined(room.room_id):
            logger.warning("[Matrix] Dropping message — could not join %s", room.room_id)
            return

        # NOTE: intentional drift from bot.py — dict-spread rebuild (the bot
        # deep-copies event.source). See client_bootstrap module docstring.
        if body != event.body:
            event = RoomMessageText.from_dict(
                {**event.source, "content": {**event.source.get("content", {}), "body": body}},
                room.room_id,
                event.sender,
                event.server_timestamp,
            )

        from src.matrix_client.chat_commands import is_ws_command, try_handle_matrix_chat_command

        if is_ws_command(body):

            async def _run_ws_command() -> None:
                await try_handle_matrix_chat_command(
                    root,
                    body,
                    send_text=lambda text: _send_plain_text(room.room_id, text),
                    room_id=room.room_id,
                )

            matrix_dispatcher.schedule_unlocked(
                _run_ws_command(),
                label="ws-command",
            )
            return

        from src.matrix_client.ingress_helpers import capture_matrix_turn_binding, matrix_view_session_key

        # 与 bot.py 对齐：按 matrix view 串行，不跟 cli 前台漂移。
        bind_coara, bind_ws_id = capture_matrix_turn_binding(root)
        fg_key = matrix_view_session_key(root) or str(bind_ws_id or "")
        matrix_dispatcher.schedule(
            _process_text_message(room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id),
            label="matrix-text",
            session_key=fg_key,
            on_busy=lambda: _send_plain_text(
                room.room_id,
                BUSY_DROP_NOTICE,
            ),
        )

    async def on_unknown_room_message(room: MatrixRoom, event: RoomMessageUnknown) -> None:
        """自定义 msgtype 的 m.room.message（nio RoomMessageUnknown）。

        审批回执专用入口：m.coara.approval_reply 直接进 ApprovalCenter.resolve
        （调度前执行——持锁等待的审批回合靠它释放），其余未知 msgtype 忽略。
        """
        from src.matrix_client.approval_bridge import handle_approval_reply_event

        content = getattr(event, "content", None)
        if not isinstance(content, dict):
            source = getattr(event, "source", None)
            if isinstance(source, dict):
                content = source.get("content")
        handle_approval_reply_event(
            sender=event.sender,
            bot_user_id=bot_user_id,
            content=content,
        )

    async def on_file(room: MatrixRoom, event: RoomMessageFile) -> None:
        from src.matrix_client.ingress_helpers import should_skip_matrix_self_event

        if should_skip_matrix_self_event(sender=event.sender, bot_user_id=config["user"]):
            return
        # 保序：文件到达前先冲刷该房间聚合中的图片批次
        from src.matrix_client.media_batch import media_batch_aggregator

        media_batch_aggregator.flush_now(room.room_id)
        if not await _ensure_joined(room.room_id):
            logger.warning("[Matrix] Dropping file — could not join %s", room.room_id)
            return
        from src.matrix_client.ingress_helpers import capture_matrix_turn_binding, matrix_view_session_key

        bind_coara, bind_ws_id = capture_matrix_turn_binding(root)
        fg_key = matrix_view_session_key(root) or str(bind_ws_id or "")
        matrix_dispatcher.schedule(
            _process_media_message(room, event, bind_coara=bind_coara, bind_ws_id=bind_ws_id),
            label="matrix-file",
            session_key=fg_key,
            on_busy=lambda: _send_plain_text(
                room.room_id,
                BUSY_DROP_NOTICE,
            ),
        )

    # 图片批量聚合的回调任务集合（防 GC 回收协程，完成即丢弃）
    media_batch_tasks: set = set()

    async def on_image(room: MatrixRoom, event: RoomMessageImage) -> None:
        """图片消息：回调入口直接下载组块进批量聚合器，不占调度锁、免 BUSY_DROP。"""
        from src.matrix_client.ingress_helpers import (
            capture_matrix_turn_binding,
            guest_room_allowed,
            matrix_view_session_key,
            resolve_matrix_trust_level,
            should_skip_matrix_self_event,
        )
        from src.matrix_client.media_inbound import build_media_batch_handler

        if should_skip_matrix_self_event(sender=event.sender, bot_user_id=config["user"]):
            return
        trust_level = resolve_matrix_trust_level(event.sender, cli_owner=ingress_host.cli_owner)
        if trust_level == "untrusted" and not guest_room_allowed(room.room_id):
            return

        def _deliver_batch(room_id: str, blocks: list, caption: str) -> None:
            bind_coara, bind_ws_id = capture_matrix_turn_binding(root)
            fg_key = matrix_view_session_key(root) or str(bind_ws_id or "")
            matrix_dispatcher.schedule(
                _deliver_media_batch(room_id, blocks, caption, bind_coara=bind_coara, bind_ws_id=bind_ws_id),
                label="matrix-image-batch",
                session_key=fg_key,
                on_busy=lambda: _send_plain_text(room_id, BUSY_DROP_NOTICE),
            )

        handler = build_media_batch_handler(
            client,
            root,
            trust_level=trust_level,
            send_room_text=_send_plain_text,
            deliver_batch=_deliver_batch,
        )
        task: asyncio.Task[None] = asyncio.ensure_future(handler(room, event))
        media_batch_tasks.add(task)
        task.add_done_callback(media_batch_tasks.discard)

    async def _deliver_media_batch(
        room_id: str,
        blocks: list,
        caption: str,
        *,
        bind_coara=None,
        bind_ws_id: str | None = None,
    ) -> None:
        from src.coara.background_activity import background_work_active
        from src.matrix_client.media_inbound import _deliver_image_turn
        from src.matrix_client.turn_signal import matrix_turn_scope, turn_send_stats

        # 正文 chunk 失败计数：批次正文走 stats.send，结束信封带 chunk_lost 标记
        raw_send_chunk = ingress_host.send_chunk
        turn_stats = turn_send_stats(raw_send_chunk)
        turn_send = turn_stats.send if turn_stats is not None else None
        if turn_send is None:
            turn_send = raw_send_chunk
        async with matrix_turn_scope(
            room_id,
            send_chunk=raw_send_chunk,
            background_active=background_work_active,
            send_stats=turn_stats,
        ):
            await _deliver_image_turn(
                root,
                room_id,
                caption,
                blocks,
                trust_level="owner",
                send_chunk=turn_send,
                echo_tool_summary_local=ingress_host.echo_tool_summary_local,
                bind_coara=bind_coara,
                bind_ws_id=bind_ws_id,
            )
            # 批次回合收尾后遗留的接续输入立即排空（与文本/媒体路径一致）
            turn_coara = bind_coara if bind_coara is not None else root.foreground_coara
            if turn_coara is not None:
                from src.matrix_client.inbound_handlers import _drain_leftover_continuations

                room = client.rooms.get(room_id)
                if room is not None:
                    await _drain_leftover_continuations(
                        ingress_host,
                        room,
                        turn_coara=turn_coara,
                        turn_ws_id=bind_ws_id,
                        trust_level="owner",
                    )

    client.add_event_callback(on_invite, InviteMemberEvent)
    client.add_event_callback(on_message, RoomMessageText)
    client.add_event_callback(on_file, RoomMessageFile)
    client.add_event_callback(on_image, RoomMessageImage)
    client.add_event_callback(on_unknown_room_message, RoomMessageUnknown)

    # 审批走自定义 msgtype：登记房间事件发送通道（进程内单 client，覆盖式幂等）
    from src.matrix_client.approval_bridge import register_approval_room_sender
    from src.matrix_client.send_guard import matrix_room_send_content

    async def _send_room_content(room_id: str, content: dict) -> bool:
        return await matrix_room_send_content(client, room_id, content)

    register_approval_room_sender(_send_room_content)

    # Full catch-up sync before long-poll (delivers post-restart timeline; join invites).
    await _join_pending_invites()

    # Restart invalidation sweep: vault unlock cards sent by a previous
    # process are dead (approval futures live in ApprovalCenter and settle
    # via m.coara.approval_resolved). Notify the room once and clear the
    # record so phone users re-issue the request.
    from src.matrix_client.pending_interactions import notify_stale_pending_interactions

    try:
        _stale_notified = await notify_stale_pending_interactions(_send_text)
        if _stale_notified:
            console.print(f"[dim][Matrix] 已向房间通知 {_stale_notified} 条重启前未完成的交互请求失效[/dim]")
    except Exception as exc:
        logger.warning(f"[Matrix] stale pending-interaction sweep failed: {exc}")

    def _save_sync_token() -> None:
        from src.matrix_client.sync_token import save_gomatrix_sync_token

        save_gomatrix_sync_token(token_path, getattr(client, "next_batch", None))

    def _on_sync_batch(_token: str) -> None:
        _save_sync_token()

    def _report_sync_error(err: object) -> None:
        from src.matrix_client.sync_token import is_invalid_sync_token_error

        if is_invalid_sync_token_error(err):
            # Token reset + re-park is owned by run_matrix_sync_loop.
            console.print("[yellow][Matrix] sync token 无效，已重置并重新定位游标（不回放历史）[/yellow]")
        console.print(f"[yellow][Matrix] Sync error ({err!r}), retrying...[/yellow]")

    def _on_link_change(up: bool) -> None:
        if up:
            console.print("[green][Matrix] homeserver 链路已恢复[/green]")
        else:
            console.print(
                "[yellow][Matrix] homeserver 链路中断（探活连续失败），消息收发暂停，恢复后会自动续上[/yellow]"
            )

    try:
        await run_matrix_client_sync_loop(
            client,
            on_batch_saved=_on_sync_batch,
            on_sync_error=_report_sync_error,
            on_link_change=_on_link_change,
            token_path=token_path,
            label="Matrix CLI",
            dispatcher=matrix_dispatcher,
        )
    finally:
        if model_switch_sub is not None:
            model_switch_sub.unsubscribe()
        if session_started_sub is not None:
            session_started_sub.unsubscribe()
        if registry_changed_sub is not None:
            registry_changed_sub.unsubscribe()
        # Close the client first: aborts any in-flight 30s sync long-poll so
        # the dispatcher drain below cannot hang on sync network IO.
        from src.matrix_client.send_guard import is_matrix_client_logged_in

        with contextlib.suppress(asyncio.TimeoutError, Exception):
            if is_matrix_client_logged_in(client):
                await asyncio.wait_for(client.logout(), timeout=2.0)
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(client.close(), timeout=2.0)
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(file_bridge.aclose(), timeout=2.0)
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await matrix_dispatcher.drain(timeout=1.0)
        with contextlib.suppress(Exception):
            _save_sync_token()
        console.print("[dim][Matrix] Disconnected[/dim]")

"""Shared helpers for Matrix remote text ingress."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.coara.turn_context import turn
from src.core.logger import logger
from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL
from src.matrix_client.response_stream import LocalToolSummaryFn, stream_coara_reply_to_matrix

SendChunkFn = Callable[[str, str], Awaitable[None]]
SendTextFn = Callable[[str, str], Awaitable[None]]


def should_skip_matrix_self_event(*, sender: str, bot_user_id: str) -> bool:
    """True only for our own Matrix echoes (never drop remote senders by age)."""
    return sender == bot_user_id


def is_any_coara_envelope(body: str) -> bool:
    """True for any ``[COARA_*]`` envelope shape"""
    from src.matrix_client.envelope_spec import is_any_coara_envelope as _impl

    return _impl(body)


def prefilter_matrix_text_event(
    *,
    root: Any,
    room_id: str,
    body: str,
    client: Any | None = None,
    send_text: Any | None = None,
) -> bool:
    """Return True when side-channel handlers consumed the event (do not schedule agent)."""
    from src.coara.mobile_sync import try_handle_mobile_sync_query
    from src.coara.updates_matrix_sync import is_updates_control_message
    from src.matrix_client.chat_commands import is_new_session_command, is_stop_command
    from src.matrix_client.collect_bridge import is_collect_message, schedule_handle_collect

    # 文本层不再识别任何 [COARA_APPROVAL_*] 信封——旧版残留由
    if is_collect_message(body):
        schedule_handle_collect(root, room_id, body, client=client, send_text=send_text)
        return True
    if is_updates_control_message(body):
        return True
    from src.matrix_client.update_cmd_bridge import is_update_cmd_message, schedule_handle_update_cmd

    if is_update_cmd_message(body):
        schedule_handle_update_cmd(root, room_id, body)
        return True
    if try_handle_mobile_sync_query(root, room_id, body):
        return True
    if is_any_coara_envelope(body):
        # 前向兼容：新版手机可能发来本运行时不认识的 [COARA_*] 信封
        from src.core.logger import logger

        logger.debug(f"Ignoring unrecognized COARA envelope: {body.strip().splitlines()[0][:60]}")
        return True
    if is_new_session_command(body):
        # 换会话只停回合，不连带杀后台（与 start_new_session 的 cancel_delegates=False 同口径）。
        matrix_view_coara(root).interrupt_current_turn(
            "new_session", interrupt_source="matrix_new_command", cancel_delegates=False
        )
    if is_stop_command(body):
        # 征求同意门（用户裁决 2026-09-24）：它端回合在跑时打断先经审批征得同意；
        # 获批后打断端经段机制获得会话所有权（与输入同权）。prefilter 是同步入口，
        # 审批是异步等待——调为后台任务，房间侧立即回执「已收到」。
        _schedule_matrix_stop_with_gate(root, room_id, send_text)
        return True
    return False


# 防 GC：/stop 同意门后台任务持有至完成
_matrix_stop_gate_tasks: set[Any] = set()


def _schedule_matrix_stop_with_gate(root: Any, room_id: str, send_text: Any) -> None:
    """异步执行 matrix /stop：先过征求同意门，获批后打断并转移所有权。"""
    import asyncio

    async def _run() -> None:
        from src.coara.commands.registry import confirm_cross_end_action
        from src.core.logger import logger
        from src.matrix_client.chat_commands import _matrix_room_channel

        target = matrix_view_coara(root)
        # send_text 入口是 (room_id, body)；房间通道要 1-arg body
        bound: Any | None = None
        if room_id and send_text is not None:

            async def _one(body: str) -> Any:
                return await send_text(room_id, body)

            bound = _one
        channel = _matrix_room_channel(room_id, bound) if bound is not None else None
        try:
            approved = await confirm_cross_end_action(
                target,
                origin_source="matrix",
                verb="打断当前回合",
                detail="打断后手机端将获得该会话的所有权。",
                interaction_channel=channel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"matrix stop gate failed: {exc}")
            approved = False
        if approved:
            target.interrupt_current_turn(
                "user_stop",
                interrupt_source="matrix_stop_command",
                take_ownership_source="matrix",
                take_ownership_channel_id=room_id,
            )

    task = asyncio.create_task(_run())
    _matrix_stop_gate_tasks.add(task)
    task.add_done_callback(_matrix_stop_gate_tasks.discard)


def extract_quoted_image_marker(body: str) -> tuple[str, str | None]:
    """文本消息里的引用图片标记：strip 后返回（干净正文, 被引 mxc）"""
    from src.matrix_client.remote_vision import strip_quote_mxc_marker

    return strip_quote_mxc_marker(body)


async def download_quoted_image_block(client: Any, mxc_url: str) -> dict[str, Any] | None:
    """下载被引用图并组视觉块；下载失败返回 None（降级为纯文本，不阻塞消息）。"""
    from src.matrix_client.remote_vision import download_mxc_bytes, image_block_from_bytes

    data = await download_mxc_bytes(client, mxc_url)
    if not data:
        return None
    return image_block_from_bytes(data)


def replace_room_text_body(event: Any, body: str) -> Any:
    """用新正文重建文本事件；重建失败则原样返回"""
    from nio import RoomMessageText

    from src.matrix_client.remote_vision import strip_quote_mxc_marker

    source = getattr(event, "source", None)
    if not isinstance(source, dict):
        return event
    content = dict(source.get("content") or {})
    content["body"] = body
    formatted = content.get("formatted_body")
    if isinstance(formatted, str):
        cleaned, marker = strip_quote_mxc_marker(formatted)
        if marker:
            content["formatted_body"] = cleaned
    rebuilt = RoomMessageText.from_dict({**source, "content": content})
    if not isinstance(rebuilt, RoomMessageText):
        return event
    rebuilt.decrypted = bool(getattr(event, "decrypted", False))
    rebuilt.verified = bool(getattr(event, "verified", False))
    return rebuilt


def register_matrix_followup_end_channel(
    root: Any,
    coara: Any,
    *,
    room_id: str,
    send_text: Any,
    interaction_channel: Any | None = None,
    actor: str = "",
) -> None:
    """记录手机跟话的远端上下文并登记 EndRegistry 的 matrix 出站通道"""
    coara.set_deferred_remote_ctx(room_id, send_text, interaction_channel, source="matrix", actor=actor)
    end_registry = getattr(root, "end_registry", None)
    if end_registry is None:
        return
    _sess_id = str(getattr(coara, "session_id", "") or "")

    async def _matrix_followup_sender(frame: dict) -> None:
        # 帧出口与主通道共用一套（tool / diff / 子智能体折叠 / 注入信封 / 正文一把尺）——
        # 跟话通道曾因为自带一份更窄的分支，把子智能体正文当主会话消息发进房间。
        from src.matrix_client.response_stream import dispatch_matrix_end_frame

        try:
            await dispatch_matrix_end_frame(frame, room_id=room_id, send_chunk=send_text)
        except Exception:
            logger.debug("matrix followup frame send failed", exc_info=True)

    # 守卫以注册表的通道类型为准（与 CLI / web 同一判据）：该会话正有 matrix 回合在跑时，
    # 槽位是它的活性通道，不能顶替——顶替会让在跑回合的输出失去路由。跟话注入开新段后，
    # 后续输出照旧经这个 sender 回同一房间。sender 上的历史标记保留，供旧读法兜底。
    if end_registry.has("matrix", _sess_id) and not end_registry.is_followup("matrix", _sess_id):
        return
    prev = end_registry.sender_for("matrix", _sess_id)
    if prev is not None:
        end_registry.unregister("matrix", prev, _sess_id)
    _matrix_followup_sender._end_matrix_followup = True  # type: ignore[attr-defined]
    end_registry.register("matrix", _matrix_followup_sender, _sess_id, kind="followup")


def try_defer_to_continuation_input(
    root: Any,
    body: str,
    *,
    channel: str = "matrix",
    room_id: str | None = None,
    send_text: Any | None = None,
    interaction_channel: Any | None = None,
    actor: str = "",
) -> bool:
    """Buffer body as mid-turn continuation when root has an active turn."""
    stripped = body.strip()
    if not stripped or stripped.startswith("/"):
        return False
    coara = matrix_view_coara(root)
    if not coara.has_active_turn():
        return False
    # 入站裸文本入队（来源标签已废弃）；注入时仅套 <接续输入>（见 turn_orchestrator）。
    payload = stripped
    if channel == "matrix" and room_id and send_text is not None:
        register_matrix_followup_end_channel(
            root,
            coara,
            room_id=room_id,
            send_text=send_text,
            interaction_channel=interaction_channel,
            actor=actor,
        )
    # source=matrix：勿继承当前回合的 source（如 background 唤醒），否则 CLI 会把手机跟话误标成 [后台] 并与「你：」
    # 双显。
    coara.submit_continuation_input(payload, source=channel or "matrix")
    return True


def try_defer_media_to_continuation_input(
    root: Any,
    caption: str,
    image_blocks: list[dict[str, Any]] | None,
    *,
    room_id: str | None = None,
    send_text: Any | None = None,
    interaction_channel: Any | None = None,
    actor: str = "",
) -> bool:
    """图片消息忙时入队：与文本接续同一队列、同一注入路径（多模态块随项携带）"""
    if image_blocks is not None and not image_blocks:
        return False
    coara = matrix_view_coara(root)
    if not coara.has_active_turn():
        return False
    if room_id and send_text is not None:
        register_matrix_followup_end_channel(
            root,
            coara,
            room_id=room_id,
            send_text=send_text,
            interaction_channel=interaction_channel,
            actor=actor,
        )
    coara.submit_continuation_input(caption.strip(), image_blocks=image_blocks or None, source="matrix")
    return True


def resolve_matrix_trust_level(sender: str, *, cli_owner: bool = False) -> str:
    """本机自用 homeserver 上的发送者一律按 owner——gomatrix 是私人的，外人进不来；
    用户口径「现在就全部是内部」。owner_matrix_ids 保留仅为未来多主预留，
    当前不作为门槛。"""
    return "owner"


def guest_room_allowed(room_id: str) -> bool:
    """访客（非 owner 发送者）是否可在该房间互动"""
    from src.core.config import config_manager

    rooms: list[str] = ["*"]
    try:
        cfg = config_manager.config
        matrix_cfg = getattr(cfg, "matrix", None) if cfg else None
        raw = getattr(matrix_cfg, "guest_rooms", None)
        if raw is not None:
            rooms = [str(r).strip() for r in raw if str(r).strip()]
    except Exception as exc:
        # 每条入站消息都会触发，高频路径只留 debug，避免刷屏
        logger.debug(f"读取 matrix.guest_rooms 配置失败，按默认全放行：{exc}")
    if "*" in rooms:
        return True
    return room_id in rooms


def workspace_allows_untrusted(root: Any, workspace_id: str | None = None) -> bool:
    """目标空间是否对外开放：仅 ``kind=external`` 接受访客（untrusted）。"""
    from src.workspace.types import WorkspaceKind

    registry = getattr(getattr(root, "workspace_manager", None), "registry", None)
    if registry is None:
        return False
    wid = str(workspace_id or "").strip()
    if not wid:
        wid = matrix_view_session_key(root) or str(getattr(root, "_foreground_session_id", "") or "")
    if not wid:
        return False
    entry = registry.get_by_id(wid)
    if entry is None:
        return False
    return getattr(entry, "kind", None) == WorkspaceKind.EXTERNAL


def untrusted_ingress_allowed(
    root: Any,
    room_id: str,
    *,
    workspace_id: str | None = None,
) -> tuple[bool, str]:
    """访客双门：房间白名单 + 空间 kind=external。返回 (允许, 拒绝原因码)。"""
    if not guest_room_allowed(room_id):
        return False, "guest_room"
    if not workspace_allows_untrusted(root, workspace_id):
        return False, "workspace_kind"
    return True, ""


def bind_matrix_active_room(
    *,
    root: Any,
    room_id: str,
    file_bridge: Any,
    coara_home: Path | str | None,
) -> None:
    """Bind active Matrix room for notify, file send defaults, and optional active.json."""
    file_bridge.set_current_room(room_id)
    root.matrix_notify.remember_remote_room(room_id, coara_home=coara_home)
    if coara_home:
        from src.coara.workspace_runtime import update_active_runtime_matrix

        update_active_runtime_matrix(
            Path(coara_home),
            matrix_enabled=True,
            matrix_room_id=room_id,
        )


def register_matrix_notify_send(
    *,
    root: Any,
    client: Any,
    notify_room_id: str = "",
    coara_home: Path | str | None = None,
) -> None:
    """Wire ``root.matrix_notify`` to plain Matrix room_send (events / updates)."""
    from src.matrix_client.send_guard import matrix_room_send_text

    root.matrix_notify.load_persisted_room(coara_home)

    async def _send(room_id: str, body: str) -> bool:
        return await matrix_room_send_text(client, room_id, body)

    root.matrix_notify.register_send(_send, default_room_id=notify_room_id or None)


def matrix_join_url(homeserver: str, room_id: str) -> str:
    """Build a spec-correct /join URL (room id must be path-encoded)."""
    from urllib.parse import quote

    encoded = quote(room_id, safe="")
    return f"{homeserver.rstrip('/')}/_matrix/client/v3/rooms/{encoded}/join"


async def matrix_join_room_with_retry(
    *,
    homeserver: str,
    access_token: str,
    room_id: str,
    attempts: int = 5,
    base_delay: float = 0.12,
) -> tuple[bool, str | None]:
    """Join a room with encoding, backoff, and transient-error retries."""
    import asyncio

    from src.matrix_client.http_session import shared_matrix_http_session

    url = matrix_join_url(homeserver, room_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    last_err: str | None = None
    retry_statuses = {403, 404, 409, 429, 500, 502, 503}

    # 共享会话：重试与多次 join 复用同一连接池，不每请求新建
    session = shared_matrix_http_session()
    for attempt in range(attempts):
        async with session.post(url, headers=headers) as resp:
            if resp.status in (200, 204):
                return True, None
            body = (await resp.text()).strip()
            last_err = f"HTTP {resp.status}: {body[:240]}"
            if resp.status not in retry_statuses or attempt + 1 >= attempts:
                return False, last_err
        await asyncio.sleep(base_delay * (2**attempt))

    return False, last_err


async def matrix_raw_join(*, homeserver: str, access_token: str, room_id: str) -> bool:
    """Manually call /join when matrix-nio join parsing fails."""
    ok, _ = await matrix_join_room_with_retry(
        homeserver=homeserver,
        access_token=access_token,
        room_id=room_id,
    )
    return ok


def should_accept_matrix_invite(*, sender: str, bot_user_id: str) -> bool:
    """True when a room invite should be auto-accepted by the coara bot."""
    import re

    from src.core.config import config_manager

    owner_ids = config_manager.get_security_config().get("owner_matrix_ids", [])
    if sender in owner_ids:
        return True
    # End-anchored so e.g. "coara.local.evil.com" cannot spoof the allowed hosts.
    return bool(re.match(r"@.*:(local\.lan|coara\.local)$", sender))


def matrix_view_coara(root: Any) -> Any:
    """matrix 视图空间 coara；未独立绑定或替身 root 时回退前台"""
    pinned = getattr(root, "pinned_view_id", None)
    if callable(pinned):
        try:
            view_id = pinned("matrix")
        except Exception:
            view_id = None
    else:
        view_id = getattr(root, "_matrix_view_workspace_id", None)
    if isinstance(view_id, str) and view_id:
        resolver = getattr(root, "resolve_matrix_view_coara", None)
        if callable(resolver):
            try:
                return resolver()
            except Exception:
                pass
    return root.foreground_coara


def matrix_view_session_key(root: Any) -> str:
    """matrix 视图 workspace_id（调度串行 / 活动时钟）；对外读统一走 view_workspace_id。"""
    view_fn = getattr(root, "view_workspace_id", None)
    if callable(view_fn):
        try:
            return str(view_fn("matrix") or "")
        except Exception:
            return ""
    return str(getattr(root, "matrix_view_workspace_id", "") or "")


def capture_matrix_turn_binding(root: Any) -> tuple[Any | None, str | None]:
    """Capture ``(matrix_view_coara, matrix_view_session_id)`` at schedule time."""
    try:
        coara = matrix_view_coara(root)
    except Exception:
        return None, None
    wid = matrix_view_session_key(root) or getattr(root, "_foreground_session_id", None)
    return coara, wid


async def deliver_remote_text_to_coara(
    root: Any,
    room_id: str,
    event_body: str,
    *,
    trust_level: str,
    send_chunk: SendChunkFn,
    send_text: SendTextFn,
    echo_tool_summary_local: LocalToolSummaryFn | None = None,
    bind_coara: Any | None = None,
    bind_ws_id: str | None = None,
    actor: str = "",
) -> None:
    """Wrap remote ingress, set turn context, and stream coara reply to Matrix"""
    from src.coara.updates_matrix_sync import is_updates_control_message

    if is_updates_control_message(event_body):
        return

    # 入站不再包内容标签：标签在注入 message_history 时由内核按 source=matrix 现包。
    from src.coara.commands.report import has_pending_report
    from src.matrix_client.chat_commands import normalize_remote_command_body

    body_norm = normalize_remote_command_body(event_body)
    if not body_norm.startswith("/") and not has_pending_report(root):
        try:
            wid = matrix_view_session_key(root)
            if not wid and bind_ws_id:
                wid = str(bind_ws_id)
        except Exception:
            wid = str(bind_ws_id or "")
        root.record_user_activity(workspace_id=wid or None)
    async with turn(
        "matrix",
        channel_id=room_id,
        send_text=send_text,
        interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
        actor=actor,
    ):
        await stream_coara_reply_to_matrix(
            root,
            event_body,
            room_id=room_id,
            trust_level=trust_level,
            send_chunk=send_chunk,
            echo_tool_summary_local=echo_tool_summary_local,
            bind_coara=bind_coara,
            bind_ws_id=bind_ws_id,
        )

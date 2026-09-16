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
    """True for any ``[COARA_*]`` envelope shape — the catch-all after all bridges.

    Checked AFTER every known-envelope bridge in ``prefilter_matrix_text_event``;
    anything still matching is either a handler miss or from a newer/older app
    build, and must not be delivered to the agent as chat text.

    「像信封」的判据（含已知标签）与 Android 端 ``EnvelopeSpec.isAnyEnvelope`` 同源，
    均由 ``scripts/dev/gen_envelopes.py`` 从 ``docs/protocol/coara-envelopes.json`` 生成。
    注意与 ``envelope_spec.is_unrecognized_coara_envelope``（只认真·未知标签）区分：
    服务端 handler 按序消费，跑到这里仍匹配的一律吞，故此处取 catch-all 语义。
    """
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
    from src.matrix_client.vault_bridge import try_resolve_vault_reply

    # 审批回执已改走自定义 msgtype 事件（m.coara.approval_reply），由
    # RoomMessageUnknown 事件层回调路由进 ApprovalCenter（见 approval_bridge）；
    # 文本层不再识别任何 [COARA_APPROVAL_*] 信封——旧版残留由
    # is_unrecognized_coara_envelope 静默吞掉，不落为聊天文本。
    if try_resolve_vault_reply(root, room_id, body):
        return True
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
        # （如目录树同步移除后旧 APK 仍发的 [COARA_DIRTREE]）；以及所有已知
        # 信封的 handler 漏网情形。一律静默吞掉，绝不能落到 LLM 当聊天文本
        from src.core.logger import logger

        logger.debug(f"Ignoring unrecognized COARA envelope: {body.strip().splitlines()[0][:60]}")
        return True
    if is_new_session_command(body):
        matrix_view_coara(root).interrupt_current_turn("new_session", interrupt_source="matrix_new_command")
    if is_stop_command(body):
        matrix_view_coara(root).interrupt_current_turn("user_stop", interrupt_source="matrix_stop_command")
        return True
    return False


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
    """Buffer body as mid-turn continuation when root has an active turn.

    跟话与开新回合同源：入接续队列、按 source 开段、注入 ``message_history``。
    手机端正式用户行即房间里用户已发的那条 Matrix 消息（不再另造旁路气泡）。
    EndRegistry 只负责段切到 matrix 后的**助手正文/工具**回投房间。

    The body is stored bare; post-turn leftovers are re-delivered by the ingress
    host as-is.
    The deferred remote context (``room_id`` / ``send_text`` /
    ``interaction_channel``) is recorded on the coara so the turn loop can
    re-apply ``turn`` ContextVars while consuming the input — without
    this, approvals on a mid-turn continuation fall
    back to local or auto-allow instead of reaching the phone.
    """
    stripped = body.strip()
    if not stripped or stripped.startswith("/"):
        return False
    coara = matrix_view_coara(root)
    if not coara.has_active_turn():
        return False
    # 入站裸文本入队（来源标签已废弃）；注入时仅套 <接续输入>（见 turn_orchestrator）。
    payload = stripped
    if channel == "matrix" and room_id and send_text is not None:
        coara.set_deferred_remote_ctx(
            room_id, send_text, interaction_channel, source="matrix", actor=actor
        )
        # 登记 EndRegistry 通道：手机跟话后正文按注入段归属投递（段=matrix 时进
        # 手机房间），否则段切走后 _route_chunk_to_current_end 查无通道静默丢弃
        # ——输出跟 source 语义的内核分发需要各参与端提供出站通道。
        end_registry = getattr(root, "end_registry", None)
        if end_registry is not None:
            _sess_id = str(getattr(coara, "session_id", "") or "")

            async def _matrix_followup_sender(frame: dict) -> None:
                if frame.get("kind") == "tool":
                    # 工具行投 [COARA_TOOL] 信封（手机端渲染成工具行）；不能当正文
                    # 直发 label，也不能丢——跟话回合的工具动作同样要看得见。
                    try:
                        from src.matrix_client.tool_bridge import build_matrix_tool_message

                        message = build_matrix_tool_message(frame)
                        if message:
                            await send_text(room_id, message)
                    except Exception:  # noqa: BLE001
                        logger.debug("matrix followup tool envelope send failed", exc_info=True)
                    return
                if frame.get("kind") == "diff":
                    try:
                        from src.coara.tool_output.pipeline import deserialize_display_blocks
                        from src.matrix_client.diff_bridge import build_matrix_diff_message

                        blocks = deserialize_display_blocks(frame.get("display_blocks"))
                        diff_message = build_matrix_diff_message(
                            blocks,
                            tool_call_id=str(frame.get("tool_call_id") or ""),
                            parent_tool_call_id=str(frame.get("parent_tool_call_id") or ""),
                        )
                        if diff_message:
                            await send_text(room_id, message)
                    except Exception:
                        logger.debug("matrix followup diff send failed", exc_info=True)
                    return
                chunk = str(frame.get("text") or "")
                if not chunk or not chunk.strip():
                    return
                try:
                    await send_text(room_id, chunk)
                except Exception:
                    logger.debug("matrix followup chunk send failed", exc_info=True)

            end_registry.register("matrix", _matrix_followup_sender, _sess_id)
    # source=matrix：勿继承当前回合的 source（如 background 唤醒），否则 CLI
    # 会把手机跟话误标成 [后台] 并与「你：」双显。
    coara.submit_continuation_input(payload, source=channel or "matrix")
    return True


def try_defer_media_to_continuation_input(
    root: Any,
    caption: str,
    image_blocks: list[dict[str, Any]],
    *,
    room_id: str | None = None,
    send_text: Any | None = None,
    interaction_channel: Any | None = None,
    actor: str = "",
) -> bool:
    """图片消息忙时入队：与文本接续同一队列、同一注入路径（多模态块随项携带）。

    媒体消息此前裸奔——忙时直闯 process_message 堵在回合锁外，caption 与
    image_blocks 分离，文本或许经别的路径入队、图片随被丢弃的协程消失。
    入队后由当前回合迭代头 drain 注入（turn_orchestrator 组多模态），回合
    结束仍未消化的 leftover 经 dispatch_leftover_item 开新回合重投，图片不丢。
    """
    if not image_blocks:
        return False
    coara = matrix_view_coara(root)
    if not coara.has_active_turn():
        return False
    if room_id and send_text is not None:
        coara.set_deferred_remote_ctx(
            room_id, send_text, interaction_channel, source="matrix", actor=actor
        )
    coara.submit_continuation_input(caption.strip(), image_blocks=image_blocks, source="matrix")
    return True


def resolve_matrix_trust_level(sender: str, *, cli_owner: bool = False) -> str:
    """Trust = configured ``owner_matrix_ids`` membership; CLI 模式不再全员 owner。

    旧版 CLI 模式（cli_owner=True）把任意发送者当 owner：经 Cloudflare tunnel
    公网暴露时，任何注册到该 homeserver 的用户发消息即获 owner 级工具面。
    现统一走配置名单；未配置名单时 CLI 模式保持旧行为（本机回环 homeserver
    单人场景），一旦名单存在即严格匹配。
    """
    from src.core.config import config_manager

    owner_ids = config_manager.get_security_config().get("owner_matrix_ids", [])
    if sender in owner_ids:
        return "owner"
    if cli_owner and not owner_ids:
        return "owner"
    return "untrusted"


def guest_room_allowed(room_id: str) -> bool:
    """访客（非 owner 发送者）是否可在该房间互动。

    ``matrix.guest_rooms``：``["*"]`` 全部放行（默认，等同改造前行为）；
    空列表 = 全面拒绝访客；否则按房间 ID 精确匹配。owner 名单成员不受
    此限制（任何房间都是 owner）。
    """
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
    """True when a room invite should be auto-accepted by the coara bot.

    同域即接受在 tunnel 公网暴露下等于「任意注册用户可入房」——改为只接受
    owner 名单成员与本地保留域的邀请；其他同域用户需用户在客户端手动拉人。
    """
    import re

    from src.core.config import config_manager

    owner_ids = config_manager.get_security_config().get("owner_matrix_ids", [])
    if sender in owner_ids:
        return True
    # End-anchored so e.g. "coara.local.evil.com" cannot spoof the allowed hosts.
    return bool(re.match(r"@.*:(local\.lan|coara\.local)$", sender))


def matrix_view_coara(root: Any) -> Any:
    """matrix 视图空间 coara；未独立绑定或替身 root 时回退前台。

    读 pin 走 :meth:`RootCoara.pinned_view_id`（真 str 才算已 pin；MagicMock 回退）；
    替身无该方法时回退读 ``_matrix_view_workspace_id`` 存储字段。
    """
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
    """Capture ``(matrix_view_coara, matrix_view_session_id)`` at schedule time.

    Handlers run later, after the dispatcher lock is acquired; binding at
    schedule time keeps a queued message on the workspace the user was looking
    at when they sent it, even if a ``/ws`` switch happens while it waits.
    阶段3：matrix 是独立视图端——捕获 matrix 自己的视图空间（与全局前台解耦），
    不再随 CLI/Web 切前台漂移。Returns ``(None, None)`` when no view session is
    bound — handlers then fall back to execution-time view reads (legacy behavior).
    """
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
    """Wrap remote ingress, set turn context, and stream coara reply to Matrix.

    *bind_coara* / *bind_ws_id* pin the turn to the scheduling-time session —
    see :func:`stream_coara_reply_to_matrix`.
    *actor* is the Matrix sender MXID that may settle approvals for this turn.
    """
    from src.coara.updates_matrix_sync import is_updates_control_message

    if is_updates_control_message(event_body):
        return

    # 入站不再包内容标签：标签在注入 message_history 时由内核按 source=matrix 现包。
    # Only real chat refreshes idle/staleness clocks — not /ws /new /….
    # Pending /report descriptions are also excluded (consumed before LLM).
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

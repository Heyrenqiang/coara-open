"""Parameterized Matrix text/media ingress handlers (bot + CLI matrix_runner)."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.coara.background_activity import background_work_active
from src.core.logger import logger
from src.matrix_client.ingress_helpers import (
    bind_matrix_active_room,
    deliver_remote_text_to_coara,
    guest_room_allowed,
    resolve_matrix_trust_level,
)
from src.matrix_client.response_stream import LocalToolSummaryFn
from src.matrix_client.turn_signal import matrix_turn_scope, turn_send_stats

SendChunkFn = Callable[[str, str], Awaitable[None]]
SendTextFn = Callable[[str, str], Awaitable[None]]
InboundPreviewFn = Callable[[Any, Any], Awaitable[None]]
ReportErrorFn = Callable[[str, Exception | None], Awaitable[None]]

# 访客被拒通知去重：同一 (房间, 发送者) 每进程只提醒一次，避免刷屏
_guest_reject_notified: set[tuple[str, str]] = set()


async def _notify_guest_rejected(host: MatrixInboundHost, room_id: str, sender: str) -> None:
    """访客在非白名单房间发言：回一句说明并忽略（每房间每发送者只提醒一次）。"""
    key = (room_id, sender)
    if key in _guest_reject_notified:
        return
    _guest_reject_notified.add(key)
    logger.info(f"[Security] Guest {sender} rejected in room {room_id}（不在 matrix.guest_rooms 白名单）")
    with contextlib.suppress(Exception):
        await host.send_room_text(
            room_id,
            f"{sender} 是外部访客，本房间未开放访客互动（不在 matrix.guest_rooms 白名单内），消息已忽略。",
        )


# Cap leftover continuation drain iterations so an event storm + slow LLM
# cannot keep the handler (and the dispatcher lock) forever. Unprocessed
# items are requeued — the next inbound message picks them up.
_MAX_LEFTOVER_ITERATIONS = 8


async def _drain_leftover_continuations(
    host: MatrixInboundHost,
    room: Any,
    *,
    turn_coara: Any,
    turn_ws_id: str | None,
    trust_level: str,
) -> None:
    """Drain post-turn leftover continuation inputs on the *origin* session.

    Leftover = 回合已结束后仍在队列的项 → 按每条 ``source`` 开**新回合**，
    不绑死本 Matrix 收尾端（web/cli 来源走对应通道）。
    """
    from src.coara.continuation_leftover import dispatch_leftover_item
    from src.coara.turn_context import turn
    from src.core.message_tags import is_preformatted_injection
    from src.core.types import unpack_continuation_item
    from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL
    from src.matrix_client.response_stream import stream_coara_reply_to_matrix

    async def _run_matrix(body: str, image_blocks: list | None = None) -> None:
        if image_blocks:
            async with turn(
                "matrix",
                channel_id=room.room_id,
                send_text=host.send_text,
                interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
            ):
                await stream_coara_reply_to_matrix(
                    host.root,
                    body,
                    room_id=room.room_id,
                    trust_level=trust_level,
                    send_chunk=host.send_chunk,
                    image_blocks=image_blocks,
                    echo_tool_summary_local=host.echo_tool_summary_local,
                    bind_coara=turn_coara,
                    bind_ws_id=turn_ws_id,
                )
            return
        await deliver_remote_text_to_coara(
            host.root,
            room.room_id,
            body,
            trust_level=trust_level,
            send_chunk=host.send_chunk,
            send_text=host.send_text,
            echo_tool_summary_local=host.echo_tool_summary_local,
            bind_coara=turn_coara,
            bind_ws_id=turn_ws_id,
        )

    async def _run_web(text: str, image_blocks: list | None = None) -> None:
        from src.coara.continuation_leftover import _dispatch_web_via_root

        await _dispatch_web_via_root(
            host.root,
            turn_coara,
            text,
            image_blocks,
            bind_ws_id=turn_ws_id,
        )

    leftover = turn_coara.drain_continuation_inputs()
    iterations = 0
    while leftover and iterations < _MAX_LEFTOVER_ITERATIONS:
        iterations += 1
        for _item in leftover:
            text, image_blocks = unpack_continuation_item(_item)
            if is_preformatted_injection(text):
                # 已带标签的系统注入：仍走 matrix 流式（与历史行为一致）
                await stream_coara_reply_to_matrix(
                    host.root,
                    text,
                    room_id=room.room_id,
                    trust_level=trust_level,
                    send_chunk=host.send_chunk,
                    echo_tool_summary_local=host.echo_tool_summary_local,
                    bind_coara=turn_coara,
                    bind_ws_id=turn_ws_id,
                )
                continue
            await dispatch_leftover_item(
                host.root,
                turn_coara,
                _item,
                finishing_source="matrix",
                bind_ws_id=turn_ws_id,
                trust_level=trust_level,
                run_matrix_turn=_run_matrix,
                run_web_turn=_run_web,
            )
        leftover = turn_coara.drain_continuation_inputs()
    if leftover:
        logger.warning(
            f"Leftover continuation inputs deferred after {_MAX_LEFTOVER_ITERATIONS} "
            f"iterations ({len(leftover)} item(s) requeued)"
        )
        turn_coara._continuation_inputs[:0] = leftover


@dataclass(slots=True)
class MatrixInboundHost:
    """Transport-specific callbacks; shared skeleton in :func:`process_matrix_*_message`."""

    root: Any
    client: Any
    file_bridge: Any
    coara_home: Path | str | None
    cli_owner: bool
    send_chunk: SendChunkFn
    send_text: SendTextFn
    send_room_text: SendChunkFn
    report_text_error: ReportErrorFn
    report_media_error: ReportErrorFn
    echo_tool_summary_local: LocalToolSummaryFn | None = None
    on_inbound_text: InboundPreviewFn | None = None
    on_inbound_media: InboundPreviewFn | None = None
    on_untrusted_sender: Callable[[str], None] | None = field(default=None, repr=False)
    text_error_log: str = "Error processing message"
    media_error_log: str = "Error processing media"


async def process_matrix_text_message(
    host: MatrixInboundHost,
    room: Any,
    event: Any,
    *,
    bind_coara: Any | None = None,
    bind_ws_id: str | None = None,
) -> None:
    """Bind room, optional inbound preview, deliver remote text to coara.

    *bind_coara* / *bind_ws_id* pin the whole handler (initial delivery AND
    leftover continuation drain) to the session captured at schedule time.
    Without them the foreground is read at execution time — a queued message
    would land on whatever workspace happens to be foreground when the
    dispatcher lock is released.
    """
    bind_matrix_active_room(
        root=host.root,
        room_id=room.room_id,
        file_bridge=host.file_bridge,
        coara_home=host.coara_home,
    )
    if host.on_inbound_text is not None:
        await host.on_inbound_text(room, event)

    try:
        turn_coara = bind_coara if bind_coara is not None else host.root.foreground_coara
        turn_ws_id = bind_ws_id if bind_ws_id is not None else getattr(host.root, "_foreground_session_id", None)
        # 正文 chunk 失败计数：回合内所有发送走 stats.send，结束信封带 chunk_lost 标记；
        # 信封自身仍走原始通道（不经统计包装）。
        raw_send_chunk = host.send_chunk
        turn_stats = turn_send_stats(raw_send_chunk)
        if turn_stats is not None:
            host.send_chunk = turn_stats.send  # type: ignore[assignment]  # 统计包装后的发送函数签名更宽
        async with matrix_turn_scope(
            room.room_id,
            send_chunk=raw_send_chunk,
            background_active=background_work_active,
            send_stats=turn_stats,
        ):
            trust_level = resolve_matrix_trust_level(event.sender, cli_owner=host.cli_owner)
            if trust_level == "untrusted":
                if not guest_room_allowed(room.room_id):
                    await _notify_guest_rejected(host, room.room_id, event.sender)
                    return
                if host.on_untrusted_sender is not None:
                    host.on_untrusted_sender(event.sender)

            async def _deliver(body: str) -> None:
                await deliver_remote_text_to_coara(
                    host.root,
                    room.room_id,
                    body,
                    trust_level=trust_level,
                    send_chunk=host.send_chunk,
                    send_text=host.send_text,
                    echo_tool_summary_local=host.echo_tool_summary_local,
                    bind_coara=turn_coara,
                    bind_ws_id=turn_ws_id,
                    actor=str(getattr(event, "sender", "") or ""),
                )

            await _deliver(event.body)

            # Leftover continuation inputs after the turn — drain the *origin*
            # session, not whatever is now foreground after a mid-turn /ws switch.
            await _drain_leftover_continuations(
                host,
                room,
                turn_coara=turn_coara,
                turn_ws_id=turn_ws_id,
                trust_level=trust_level,
            )
    except Exception as exc:
        logger.exception(host.text_error_log)
        await host.report_text_error(room.room_id, exc)
    finally:
        # 回合统计包装器只服务本回合：恢复原始通道，防下回合重复包装
        if turn_stats is not None and host.send_chunk is turn_stats.send:
            host.send_chunk = raw_send_chunk


async def process_matrix_media_message(
    host: MatrixInboundHost,
    room: Any,
    event: Any,
    *,
    bind_coara: Any | None = None,
    bind_ws_id: str | None = None,
) -> None:
    """Bind room, optional inbound preview, route m.image / m.file to coara.

    *bind_coara* / *bind_ws_id* pin the media target workspace and reply turn
    to the scheduling-time session (see :func:`process_matrix_text_message`).
    """
    from src.matrix_client.media_inbound import process_matrix_media_inbound

    bind_matrix_active_room(
        root=host.root,
        room_id=room.room_id,
        file_bridge=host.file_bridge,
        coara_home=host.coara_home,
    )
    if host.on_inbound_media is not None:
        await host.on_inbound_media(room, event)

    try:
        target_coara = bind_coara if bind_coara is not None else host.root.foreground_coara
        turn_ws_id = bind_ws_id if bind_ws_id is not None else getattr(host.root, "_foreground_session_id", None)
        trust_level = resolve_matrix_trust_level(event.sender, cli_owner=host.cli_owner)
        if trust_level == "untrusted" and not guest_room_allowed(room.room_id):
            await _notify_guest_rejected(host, room.room_id, event.sender)
            return
        # 图片消息在回调入口已下载聚合（不占调度锁、免 BUSY_DROP），此处只跑文件
        from src.matrix_client.media_inbound import matrix_media_meta
        from src.matrix_client.remote_vision import is_image_upload

        _mime, _filename = matrix_media_meta(event)
        if is_image_upload(_mime, _filename or getattr(event, "body", "")):
            return
        # 与文本路径一致：正文发送计数，结束信封带 chunk_lost；信封走原始通道
        raw_send_chunk = host.send_chunk
        turn_stats = turn_send_stats(raw_send_chunk)
        if turn_stats is not None:
            host.send_chunk = turn_stats.send  # type: ignore[assignment]  # 统计包装后的发送函数签名更宽
        async with matrix_turn_scope(
            room.room_id,
            send_chunk=raw_send_chunk,
            background_active=background_work_active,
            send_stats=turn_stats,
        ):
            await process_matrix_media_inbound(
                host.client,
                host.root,
                room,
                event,
                workspace_dir=Path(target_coara.workspace_dir),
                trust_level=trust_level,
                file_bridge=host.file_bridge,
                send_chunk=host.send_chunk,
                send_room_text=host.send_room_text,
                echo_tool_summary_local=host.echo_tool_summary_local,
                bind_coara=bind_coara,
                bind_ws_id=bind_ws_id,
            )
            # 与文本路径一致：媒体回合收尾后遗留在接续队列里的输入（回合末段
            # 才到达的文本）在此排空，否则会一直等到下一条入站消息才被处理
            await _drain_leftover_continuations(
                host,
                room,
                turn_coara=target_coara,
                turn_ws_id=turn_ws_id,
                trust_level=trust_level,
            )
    except Exception as exc:
        logger.exception(host.media_error_log)
        await host.report_media_error(room.room_id, exc)
    finally:
        # 回合统计包装器只服务本回合：恢复原始通道，防下回合重复包装
        if turn_stats is not None and host.send_chunk is turn_stats.send:
            host.send_chunk = raw_send_chunk

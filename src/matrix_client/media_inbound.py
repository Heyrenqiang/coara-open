"""Shared Matrix inbound media handling (m.image / m.file) for CLI and bot."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from src.coara.turn_context import turn
from src.core.workspace_layout import UPLOAD_DIR_NAME
from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL
from src.matrix_client.remote_vision import (
    build_image_blocks_from_matrix_upload,
    strip_quote_mxc_marker,
)
from src.matrix_client.response_stream import LocalToolSummaryFn, stream_coara_reply_to_matrix

SendRoomText = Callable[[str, str], Awaitable[Any]]

# Matches the gomatrix server-side MaxRequestSize (20MB).
MAX_MATRIX_UPLOAD_BYTES = 20 * 1024 * 1024


def matrix_media_content(event: Any) -> dict:
    raw = getattr(event, "source", None)
    if isinstance(raw, dict):
        inner = raw.get("content")
        if isinstance(inner, dict):
            return inner
        return raw
    return {}


def matrix_media_meta(event: Any) -> tuple[str | None, str | None]:
    content: dict[str, Any] = matrix_media_content(event) or {}
    raw_info = content.get("info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    mime_type = info.get("mimetype")
    filename = content.get("filename") or getattr(event, "filename", None)
    return mime_type, filename


async def _deliver_image_turn(
    root: Any,
    room_id: str,
    user_text: str,
    image_blocks: list[dict[str, Any]],
    *,
    trust_level: str,
    send_chunk: SendRoomText,
    echo_tool_summary_local: LocalToolSummaryFn | None,
    bind_coara: Any | None,
    bind_ws_id: str | None,
    actor: str = "",
) -> None:
    """图片批次投递：忙时入接续队列（与文本同队列同注入路径），空闲开新回合。

    忙时入队同步执行，保证批次与后续文本消息的相对顺序；空闲路径经调用方
    持有的调度器锁开回合，不会与队列中其它消息并发。
    """
    from src.matrix_client.ingress_helpers import matrix_view_session_key, try_defer_media_to_continuation_input

    async def _send_text(rid: str, body: str) -> bool:
        result = await send_chunk(rid, body)
        return result is not False

    # 忙时入队：caption 与 image_blocks 绑在同一队列项上，绝不出现「文字到了图丢了」
    if try_defer_media_to_continuation_input(
        root,
        user_text,
        image_blocks,
        room_id=room_id,
        send_text=_send_text,
        interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
        actor=actor,
    ):
        return

    root.record_user_activity(workspace_id=matrix_view_session_key(root) or None)
    async with turn(
        "matrix",
        channel_id=room_id,
        send_text=_send_text,
        interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
        actor=actor,
    ):
        await stream_coara_reply_to_matrix(
            root,
            user_text,
            room_id=room_id,
            trust_level=trust_level,
            send_chunk=send_chunk,
            image_blocks=image_blocks,
            echo_tool_summary_local=echo_tool_summary_local,
            bind_coara=bind_coara,
            bind_ws_id=bind_ws_id,
        )


def build_media_batch_handler(
    client: Any,
    root: Any,
    *,
    trust_level: str,
    send_room_text: SendRoomText,
    deliver_batch: Callable[[str, list[dict[str, Any]], str], None],
) -> Callable[[Any, Any], Awaitable[None]]:
    """构建「入回调即处理」的图片事件闭包：下载组块 → 聚合器去抖合并 → 整批投递。

    图片消息不再经 per-session 调度锁排队（排队会拆散批次、被 BUSY_DROP 丢弃）：
    下载与组块在回调任务内完成，窗口期到达的其它图片并入同一批；窗口届满由
    *deliver_batch*（宿主提供，负责经调度锁投递）一次性开回合/入队。
    """

    async def _handle(room: Any, event: Any) -> None:
        mime_type, declared_name = matrix_media_meta(event)
        caption, quoted_mxc = strip_quote_mxc_marker(event.body or "")
        # 端上无附言时 body 回落成文件名，那不算用户正文
        if caption.strip() and caption.strip() == (declared_name or "").strip():
            caption = ""

        # 视觉门控：当前模型不支持图像时直接告知，不静默占位（用户以为已发图）
        try:
            from src.core.config import config_manager as _config_manager
            from src.llm.vision import model_supports_vision
            from src.matrix_client.ingress_helpers import matrix_view_coara

            turn_coara = matrix_view_coara(root)
            _prov = getattr(turn_coara, "provider_name", "") or ""
            _mname = getattr(turn_coara, "model_name", "") or ""
            if (
                isinstance(_mname, str)
                and isinstance(_prov, str)
                and _mname
                and not model_supports_vision(
                    _mname,
                    provider_name=_prov,
                    config_manager=_config_manager,
                )
            ):
                await send_room_text(
                    room.room_id,
                    f"当前模型 {_mname} 不支持图像输入，图片未发送。请切换到视觉模型后重发。",
                )
                return
        except Exception:
            pass  # 门控自身异常不阻塞发图（fail-open）

        image_blocks = await build_image_blocks_from_matrix_upload(
            client,
            mxc_url=event.url,
            mime_type=mime_type,
            quoted_mxc=quoted_mxc,
        )
        if not image_blocks:
            await send_room_text(room.room_id, "图片下载失败。")
            return

        from src.matrix_client.media_batch import media_batch_aggregator

        media_batch_aggregator.add(
            room.room_id,
            blocks=image_blocks,
            caption=caption,
            deliver=lambda blocks, text: deliver_batch(room.room_id, blocks, text),
        )

    return _handle


async def process_matrix_media_inbound(
    client: Any,
    root: Any,
    room: Any,
    event: Any,
    *,
    workspace_dir: Path,
    trust_level: str,
    file_bridge: Any,
    send_chunk: SendRoomText,
    send_room_text: SendRoomText,
    echo_tool_summary_local: LocalToolSummaryFn | None = None,
    bind_coara: Any | None = None,
    bind_ws_id: str | None = None,
) -> None:
    """Route Matrix m.file uploads to workspace file path.

    图片消息由 :func:`build_media_batch_handler` 在回调入口处理（批量聚合），
    本函数只处理文件。忙时投递与回合开法见 :func:`_deliver_image_turn`。
    """
    from nio import DownloadResponse

    content = matrix_media_content(event)
    msgtype = content.get("msgtype", "m.file")
    sender_label = room.user_name(event.sender) if hasattr(room, "user_name") else event.sender
    logger.info(f"[Matrix] Media ({msgtype}) from {sender_label}: {(event.body or '')[:80]}")
    file_bridge.set_current_room(room.room_id)

    mime_type, declared_name = matrix_media_meta(event)
    filename = declared_name or event.body

    resp = await client.download(event.url)
    if not isinstance(resp, DownloadResponse):
        logger.warning(f"[Matrix] Failed to download file: {resp}")
        await send_room_text(room.room_id, "文件下载失败。")
        return

    if len(resp.body) > MAX_MATRIX_UPLOAD_BYTES:
        logger.warning(f"[Matrix] File too large ({len(resp.body)} bytes), rejected")
        await send_room_text(room.room_id, "文件超过 20MB 大小上限。")
        return

    workspace = workspace_dir.resolve()
    uploads_dir = workspace / UPLOAD_DIR_NAME
    uploads_dir.mkdir(exist_ok=True)
    raw_name = getattr(event, "filename", None) or filename or "upload.bin"
    # Strip any directory components so a crafted filename cannot escape uploads/.
    safe_name = Path(raw_name).name or "upload.bin"
    file_path = uploads_dir / safe_name
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    counter = 1
    while file_path.exists():
        file_path = uploads_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    file_path.write_bytes(resp.body)
    logger.info(f"[Matrix] Saved file: {file_path}")

    # 最近文件索引：你发我的文件记一条（inbound / matrix）。
    try:
        from src.matrix_client.ingress_helpers import matrix_view_session_key as _mv_key
        from src.records.recent_files import record_recent_file

        _home = getattr(root, "coara_home", None)
        if _home is not None:
            record_recent_file(
                _home,
                origin="inbound",
                end="matrix",
                name=file_path.name,
                path=str(file_path.resolve()),
                mime=mime_type or "",
                size=len(resp.body),
                workspace_id=_mv_key(root) or None,
            )
    except Exception as _exc:  # noqa: BLE001 — 索引故障不影响收文件
        logger.warning(f"recent_files record failed (matrix inbound): {_exc}")

    # 入站不再包内容标签：内核按 source=matrix 现包（inject_user_message）。
    # body 是端上的文件附言，必须随文件一起进回合（端上无附言时回落成文件名，那不算正文）
    caption, _ = strip_quote_mxc_marker(event.body or "")
    caption = caption.strip()
    if caption and caption == (declared_name or "").strip():
        caption = ""
    # 文本带绝对路径：模型据此一步 read，不必先 glob/shell 猜文件落点
    file_body = f"发送了文件（用户附件，已存入工作空间 {UPLOAD_DIR_NAME}/）: {file_path.resolve()}"
    if caption:
        file_body = f"{file_body}\n{caption}"

    async def _send_text(room_id: str, body: str) -> bool:
        result = await send_chunk(room_id, body)
        return result is not False

    from src.matrix_client.ingress_helpers import matrix_view_session_key

    root.record_user_activity(workspace_id=matrix_view_session_key(root) or None)
    async with turn(
        "matrix",
        channel_id=room.room_id,
        send_text=_send_text,
        interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
        actor=str(getattr(event, "sender", "") or ""),
    ):
        await stream_coara_reply_to_matrix(
            root,
            file_body,
            room_id=room.room_id,
            trust_level=trust_level,
            send_chunk=send_chunk,
            echo_tool_summary_local=echo_tool_summary_local,
            bind_coara=bind_coara,
            bind_ws_id=bind_ws_id,
        )

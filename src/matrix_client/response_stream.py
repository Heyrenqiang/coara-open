"""Stream coara process_message yields to Matrix as separate room messages."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from src.core.logger import logger
from src.matrix_client.chat_commands import try_handle_matrix_chat_command

SendChunkFn = Callable[[str, str], Awaitable[None]]
LocalToolSummaryFn = Callable[[str], Awaitable[None] | None]


def is_tool_summary_chunk(chunk: str) -> bool:
    """True for streamed tool summary lines (``✓ tool(...)`` / ``✗ tool 报错``)."""
    stripped = chunk.lstrip()
    return stripped.startswith("✓") or stripped.startswith("✗")


def _record_tape(root: Any, frame: dict[str, Any]) -> None:
    """把一帧落进录像带。落带是内核录制器的职责，端只交出帧与归属。"""
    try:
        from src.ui.view_recorder import record_view_frame

        record_view_frame(frame, coara_home=getattr(root, "coara_home", None))
    except Exception:  # noqa: BLE001 — 落带失败绝不中断回合
        logger.debug("matrix view record failed", exc_info=True)


def _matrix_turn_context_active(room_id: str) -> bool:
    """True when current EndChannel already targets this Matrix room with an interaction channel."""
    from src.coara.turn_context import get_end_channel, get_turn_channel

    end = get_end_channel()
    if end is None or get_turn_channel() is None:
        return False
    if str(getattr(end, "source", "") or "").strip().lower() != "matrix":
        return False
    return str(getattr(end, "channel_id", "") or "").strip() == str(room_id or "").strip()


@asynccontextmanager
async def _ensure_matrix_turn_context(room_id: str, send_chunk: SendChunkFn):
    """Ensure Matrix ``turn(...)`` ContextVars so approval can resolve room_id."""
    if _matrix_turn_context_active(room_id):
        yield
        return

    from src.coara.turn_context import get_end_channel, turn
    from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL

    async def _send_text(rid: str, body: str) -> bool:
        result = await send_chunk(rid, body)
        return result is not False

    prev = get_end_channel()
    actor = str(getattr(prev, "actor", "") or "") if prev is not None else ""

    async with turn(
        "matrix",
        channel_id=room_id,
        send_text=_send_text,
        interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
        actor=actor,
    ):
        yield


async def stream_coara_reply_to_matrix(
    root: Any,
    message: str,
    *,
    room_id: str,
    trust_level: str,
    send_chunk: SendChunkFn,
    image_blocks: list[dict] | None = None,
    echo_tool_summary_local: LocalToolSummaryFn | None = None,
    bind_coara: Any | None = None,
    bind_ws_id: str | None = None,
) -> None:
    """Forward each ``process_message`` yield to Matrix as it arrives.

    Tool summary lines are **not** sent to the room (mobile chat stays clean,
    no tool noise). When *echo_tool_summary_local* is provided (CLI host), raw
    summary lines are still echoed locally so the terminal shows tool activity
    during remote-originated turns.

    If the user switches workspace mid-turn, remaining chunks are still sent to
    Matrix — prefixed with the origin workspace name (``[shop] …``) so the
    shared room can tell which space produced them. The origin session finishes
    the turn either way. Detached chunks keep tool summaries filtered.

    *bind_coara* / *bind_ws_id* pin the turn to the session captured when the
    inbound message was scheduled. Without them the foreground is read at call
    time — wrong for queued messages and leftover continuations after a
    mid-turn switch.

    Always ensures a Matrix ``turn`` ContextVar scope around ``process_message``
    (nested no-op when the caller already opened one) so tool approvals can
    locate the room and emit ``m.coara.approval``.
    """
    if await try_handle_matrix_chat_command(
        root,
        message,
        send_text=lambda body: send_chunk(room_id, body),
        room_id=room_id,
    ):
        return

    from src.coara.commands.report import try_consume_pending_report_async
    from src.matrix_client.chat_commands import normalize_remote_command_body

    pending_result = await try_consume_pending_report_async(root, normalize_remote_command_body(message))
    if pending_result is not None:
        await send_chunk(room_id, pending_result.output)
        return

    # Generate the turn_id at the remote-turn entry so all trace events for
    # this Matrix turn correlate (process_message would generate its own
    # otherwise, which callers couldn't observe).
    turn_id = uuid.uuid4().hex
    # matrix 独立视图（D6）：回合绑定回退与 detach 判定都按 matrix 视图空间，
    # 不再以全局前台为准——CLI/Web 切前台不再 detach 手机正在看的回合。
    from src.matrix_client.ingress_helpers import matrix_view_coara, matrix_view_session_key

    turn_coara = bind_coara if bind_coara is not None else matrix_view_coara(root)
    turn_ws_id = (
        bind_ws_id
        if bind_ws_id is not None
        else (matrix_view_session_key(root) or getattr(root, "_foreground_session_id", None))
    )
    from src.coara.turn_detach import workspace_display_name

    def still_fg() -> bool:
        current = matrix_view_session_key(root) or getattr(root, "_foreground_session_id", None)
        return current == turn_ws_id

    # 可变容器：显示通道闭包与驱动循环共享 detached 前缀状态。
    _detached: dict[str, str | None] = {"prefix": None}

    def _compute_detached_prefix() -> str:
        if _detached["prefix"] is None:
            ws_name = workspace_display_name(root, str(getattr(turn_coara, "workspace_dir", "") or ""))
            _detached["prefix"] = f"[{ws_name}] " if ws_name else ""
        return _detached["prefix"] or ""

    # 注册本回合显示通道到 EndRegistry：正文 chunk 由 base.py 统一路由投递。
    # sender 闭包捕获 room_id / still_fg / detached 前缀（切空间后 [shop] 前缀
    # 语义由闭包内 still_fg 判定保留）。中途切走不注销——matrix 房间共享，detached
    # 后正文带前缀续投（与旧循环一致）。
    end_registry = getattr(root, "end_registry", None)
    _sess_id = str(getattr(turn_coara, "session_id", "") or "")

    # 录像带：手机端回合与其它端同契约——用户行、回合起止、正文与工具帧都要进带。
    # 落带由内核录制器执行，这里只按帧自身的空间与会话归属交出去。
    from src.ui.web_views import user_frame_display_text

    _tape_base: dict[str, Any] = {
        "workspace_dir": str(getattr(turn_coara, "workspace_dir", "") or ""),
        "session_id": _sess_id,
        "subject": "root",
        "source": "matrix",
        "turn_id": turn_id,
    }
    _record_tape(
        root,
        {**_tape_base, "kind": "user_message", "payload": {"content": user_frame_display_text(message)}},
    )
    _record_tape(root, {**_tape_base, "kind": "turn_start"})

    def _tape_frame(frame: dict) -> None:
        """内核帧转视图帧，与 web 端同一套 kind 与 payload 约定。"""
        kind = str(frame.get("kind") or "")
        if kind not in {"chunk", "tool", "diff", "subagent_result"}:
            # subagent_chunk 等过程帧不进带，落了带 hydrate 会把它复现成主会话正文
            return
        payload: dict[str, Any] = {}
        parent_id = str(frame.get("parent_tool_call_id") or "")
        tool_call_id = str(frame.get("tool_call_id") or "")
        if kind == "diff":
            diff_lines = frame.get("diff_lines")
            if isinstance(diff_lines, dict):
                payload["diff_lines"] = diff_lines
            if tool_call_id:
                payload["tool_call_id"] = tool_call_id
            if parent_id:
                payload["parent_tool_call_id"] = parent_id
                payload["display_blocks"] = frame.get("display_blocks")
                payload["tool_name"] = str(frame.get("tool_name") or "")
        else:
            payload["text"] = str(frame.get("text") or "")
            if parent_id:
                payload["parent_tool_call_id"] = parent_id
            if tool_call_id:
                payload["tool_call_id"] = tool_call_id
        _record_tape(root, {**_tape_base, "kind": kind, "payload": payload})

    sender: Any = None
    if end_registry is not None:

        async def sender(frame: dict) -> None:
            _tape_frame(frame)
            if frame.get("kind") == "tool":
                # 工具行：投 [COARA_TOOL] 信封（手机端渲染成工具行）；不再让 label
                # 落到 else 分支当正文散进房间。
                try:
                    from src.matrix_client.tool_bridge import build_matrix_tool_message

                    message = build_matrix_tool_message(frame)
                    if message:
                        await send_chunk(room_id, message)
                except Exception:  # noqa: BLE001
                    logger.exception("matrix tool frame send failed")
                return
            if frame.get("kind") == "diff":
                # diff 帧：构建 [COARA_DIFF] 消息发房间（原 diff_bridge 渲染逻辑收拢进通道）
                try:
                    from src.coara.tool_output.pipeline import deserialize_display_blocks
                    from src.matrix_client.diff_bridge import build_matrix_diff_message

                    blocks = deserialize_display_blocks(frame.get("display_blocks"))
                    message = build_matrix_diff_message(
                        blocks,
                        tool_call_id=str(frame.get("tool_call_id") or ""),
                        parent_tool_call_id=str(frame.get("parent_tool_call_id") or ""),
                    )
                    if message:
                        await send_chunk(room_id, message)
                except Exception:  # noqa: BLE001
                    logger.exception("matrix diff frame send failed")
                return
            # 子智能体帧（过程正文 / 最终结果）：带父标识就进房间，端上折进发起它的
            # delegate 行；无父标识按正文处理（main 会话直接说的正文也走这里）。
            parent_id = str(frame.get("parent_tool_call_id") or "")
            kind = str(frame.get("kind") or "")
            chunk = str(frame.get("text") or "")
            if not chunk.strip():
                return
            if kind in ("subagent_chunk", "subagent_result") and parent_id:
                # 子智能体正文/结果：行内包一层「这是子智能体的」标记，端上折进父行，
                # 不当主会话气泡。结构同工具行（[COARA_TOOL]{…}），父标识在 JSON 里。
                try:
                    import json as _json

                    sub_payload = {"kind": kind, "text": chunk, "parent_tool_call_id": parent_id}
                    message = f"[COARA_SUBAGENT]{_json.dumps(sub_payload, ensure_ascii=False, separators=(',', ':'))}"
                    await send_chunk(room_id, message)
                except Exception:  # noqa: BLE001
                    logger.exception("matrix subagent frame send failed")
                return
            if not still_fg():
                await send_chunk(room_id, f"{_compute_detached_prefix()}{chunk}")
                return
            await send_chunk(room_id, chunk)

        end_registry.register("matrix", sender, _sess_id)
    try:
        async with _ensure_matrix_turn_context(room_id, send_chunk):
            async for chunk in turn_coara.process_message(
                message,
                trust_level=trust_level,
                show_tool_summary=True,
                image_blocks=image_blocks,
                source="matrix",
                turn_id=turn_id,
            ):
                if not chunk.strip():
                    continue
                if is_tool_summary_chunk(chunk):
                    # 工具摘要不进 Matrix 房间（手机端噪音）；本地终端（CLI）照常回显
                    if echo_tool_summary_local is not None:
                        result = echo_tool_summary_local(chunk)
                        if asyncio.iscoroutine(result):
                            await result
                    continue
                # 正文：已注册 EndRegistry 通道时由 base.py 统一路由投递（sender 内
                # 处理 detached 前缀），循环不再自渲；无注册表（测试夹具/降级路径）
                # 时保留原自渲循环。
                if sender is not None:
                    continue
                if not still_fg():
                    await send_chunk(room_id, f"{_compute_detached_prefix()}{chunk}")
                    continue
                # Keep the turn scope open for the whole turn. Clearing it per chunk made the
                # App "..." stop after the first assistant message while Coara was
                # still working (multi-chunk / tool-loop turns). The [COARA_TURN] end
                # envelope is sent when matrix_turn_scope exits after process_message finishes.
                await send_chunk(room_id, chunk)
    finally:
        _record_tape(root, {**_tape_base, "kind": "turn_end", "payload": {"reason": "complete"}})
        if end_registry is not None and sender is not None:
            end_registry.unregister("matrix", sender, _sess_id)
        # Notify CLI spinner that the turn has completed
        from src.coara.event_bus import TraceEvent

        root.event_bus.publish(
            TraceEvent(
                coara_id=root.identity.coara_id,
                coara_name=root.identity.name,
                event_type="event_turn_complete",
                message="Matrix remote turn finished",
                payload={
                    "room_id": room_id,
                    "source": "matrix",
                    "turn_id": turn_id,
                    "session_id": turn_coara.session_id,
                    "workspace_id": turn_ws_id,
                },
            )
        )

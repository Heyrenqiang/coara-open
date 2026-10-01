"""Stream coara process_message yields to Matrix as separate room messages."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from src.core.logger import logger
from src.matrix_client.chat_commands import try_handle_matrix_chat_command

SendChunkFn = Callable[[str, str], Awaitable[Any]]
LocalToolSummaryFn = Callable[[str], Awaitable[None] | None]

SUBAGENT_ENVELOPE_PREFIX = "[COARA_SUBAGENT]"


def build_matrix_subagent_envelope(
    kind: str,
    text: str,
    parent_tool_call_id: str,
    *,
    depth: int = 0,
    subagent_id: str = "",
    coara_id: str = "",
) -> str:
    """子智能体帧 → ``[COARA_SUBAGENT]`` 信封（端上折进发起它的 delegate 工具行）

    任务描述 / 中间过程 / 最终结果三样都走这条，绝不落成主会话气泡——
    与 web / CLI 出口同一口径；可选 depth / 节点身份与 Web 折叠过程条目同形
    """
    import json as _json

    payload: dict[str, object] = {
        "kind": kind or "subagent_chunk",
        "text": text,
        "parent_tool_call_id": parent_tool_call_id,
    }
    if isinstance(depth, int) and depth > 0:
        payload["depth"] = depth
    sid = str(subagent_id or "").strip()
    if sid:
        payload["subagent_id"] = sid
    cid = str(coara_id or "").strip()
    if cid:
        payload["coara_id"] = cid
    return f"{SUBAGENT_ENVELOPE_PREFIX}{_json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"


async def dispatch_matrix_end_frame(
    frame: dict[str, Any],
    *,
    room_id: str,
    send_chunk: SendChunkFn,
) -> None:
    """把一帧内核输出投成房间消息 —— 手机链路的唯一帧出口

    - ``tool`` / ``diff`` 帧封对应信封，端上渲染成工具行 / diff 卡
    - 带 ``parent_tool_call_id`` 的帧（子智能体过程正文与最终结果）封 ``[COARA_SUBAGENT]``，
      端上折进发起它的 delegate 工具行；判据只看父标识——实例重建时子智能体正文会以
      ``kind="chunk"`` 混进主会话流（base.py 已打父标），只按 kind 判定会让它退化成正文
    - 注入信封（``<后台结果>`` / ``<系统提醒>`` …）只给模型看，不投房间
    - 其余按正文发送（空间归属由 content.coara_ws_* 标签承载，端上按空间分页）
    """
    kind = str(frame.get("kind") or "")
    if kind == "tool":
        try:
            from src.matrix_client.tool_bridge import build_matrix_tool_message

            message = build_matrix_tool_message(frame)
            if message:
                await send_chunk(room_id, message)
        except Exception:  # noqa: BLE001 — 单帧失败不打断回合
            logger.exception("matrix tool frame send failed")
        return
    if kind == "diff":
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
                await send_chunk(room_id, diff_message)
        except Exception:  # noqa: BLE001
            logger.exception("matrix diff frame send failed")
        return
    chunk = str(frame.get("text") or "")
    if not chunk.strip():
        return
    from src.core.message_tags import is_preformatted_injection

    if is_preformatted_injection(chunk):
        return
    parent_id = str(frame.get("parent_tool_call_id") or "")
    if parent_id:
        try:
            depth_raw = frame.get("depth")
            depth = int(depth_raw) if isinstance(depth_raw, (int, float)) else 0
            await send_chunk(
                room_id,
                build_matrix_subagent_envelope(
                    kind,
                    chunk,
                    parent_id,
                    depth=depth,
                    subagent_id=str(frame.get("subagent_id") or ""),
                    coara_id=str(frame.get("coara_id") or ""),
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("matrix subagent frame send failed")
        return
    if kind in ("subagent_chunk", "subagent_result"):
        # 子智能体帧缺父标识：直发房间会显示成主消息，丢掉（异常态，web/CLI 出口同尺）
        logger.warning("matrix end frame dropped: %s without parent call id", kind)
        return
    await send_chunk(room_id, chunk)


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
    """Forward each ``process_message`` yield to Matrix as it arrives"""
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

    # otherwise, which callers couldn't observe).
    turn_id = uuid.uuid4().hex
    # matrix 独立视图（D6）：回合绑定回退与 detach 判定都按 matrix 视图空间，
    from src.matrix_client.ingress_helpers import matrix_view_coara, matrix_view_session_key

    turn_coara = bind_coara if bind_coara is not None else matrix_view_coara(root)
    turn_ws_id = (
        bind_ws_id
        if bind_ws_id is not None
        else (matrix_view_session_key(root) or getattr(root, "_foreground_session_id", None))
    )

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
        parent_id = str(frame.get("parent_tool_call_id") or "")
        tool_call_id = str(frame.get("tool_call_id") or "")
        # subagent_chunk 带父标识落带（与 web make_persist 同形）；无父则丢——防复现成主会话正文
        if kind == "subagent_chunk" and not (parent_id or tool_call_id):
            return
        if kind not in {"chunk", "tool", "diff", "subagent_result", "subagent_chunk"}:
            return
        payload: dict[str, Any] = {}
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
            await dispatch_matrix_end_frame(
                frame,
                room_id=room_id,
                send_chunk=send_chunk,
            )

        end_registry.register("matrix", sender, _sess_id)
    # 空间标签绑定：本回合经 matrix_room_send_text 发出的所有消息自动带
    # coara_ws_id/coara_ws_name，手机端按空间分页过滤的唯一依据（改名不影响，
    # 端上只认 id）。回合区间外发送（命令回执、同步推送）不绑标签。
    from src.coara.turn_detach import workspace_display_name
    from src.matrix_client.send_guard import reset_matrix_ws_tag, set_matrix_ws_tag

    _ws_tag_token = set_matrix_ws_tag(
        str(turn_ws_id or ""),
        workspace_display_name(root, str(getattr(turn_coara, "workspace_dir", "") or "")),
    )
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
                # 时保留原自渲循环。
                from src.core.message_tags import is_preformatted_injection

                if is_preformatted_injection(chunk):
                    # 无端注册表时的自渲路径：注入信封同样不上屏
                    continue
                if sender is not None:
                    continue
                # App "..." stop after the first assistant message while Coara was
                await send_chunk(room_id, chunk)
    finally:
        reset_matrix_ws_tag(_ws_tag_token)
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

"""Matrix side-channel: mobile message-card collect → records/user.

App sends ``[COARA_COLLECT]``; PC writes/removes user records and ACKs with
``[COARA_COLLECT_ACK]``. Never enters the LLM turn loop.

ACK send must not rely on ``turn`` context — collect is handled in
prefilter before any turn starts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.coara.turn_context import get_turn_send_text
from src.core.logger import logger

COLLECT_START = "[COARA_COLLECT]"
COLLECT_END = "[/COARA_COLLECT]"
ACK_START = "[COARA_COLLECT_ACK]"
ACK_END = "[/COARA_COLLECT_ACK]"

_KNOWN_KEYS = frozenset({"action", "event_id", "msgtype", "title", "body", "mxc", "filename", "mime", "id"})
_ACK_TASKS: set[asyncio.Task[Any]] = set()

SendTextFn = Callable[[str, str], Awaitable[bool]]


def is_collect_message(body: str) -> bool:
    return COLLECT_START in (body or "")


def matrix_event_source_url(event_id: str) -> str:
    eid = (event_id or "").strip()
    return f"matrix:{eid}" if eid else ""


def parse_collect(body: str) -> dict[str, str] | None:
    text = (body or "").strip()
    if COLLECT_START not in text:
        return None
    start = text.find(COLLECT_START)
    end = text.find(COLLECT_END, start)
    block = text[start + len(COLLECT_START) : end if end > start else len(text)].strip()
    if not block:
        return None

    parsed: dict[str, str] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or ":" not in stripped:
            i += 1
            continue
        key, value = stripped.split(":", 1)
        key_l = key.strip().lower()
        value = value.strip()
        if key_l not in _KNOWN_KEYS:
            i += 1
            continue
        if key_l == "body":
            parts: list[str] = [value] if value else []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                nxt_s = nxt.strip()
                if ":" in nxt_s:
                    k = nxt_s.split(":", 1)[0].strip().lower()
                    if k in _KNOWN_KEYS and k != "body":
                        break
                parts.append(nxt)
                i += 1
            parsed["body"] = "\n".join(parts).strip()
            continue
        parsed[key_l] = value
        i += 1

    action = (parsed.get("action") or "add").strip().lower()
    if action not in {"add", "remove"}:
        return None
    parsed["action"] = action
    return parsed


def build_collect_ack(
    *,
    event_id: str,
    ok: bool,
    action: str = "",
    entry_id: str = "",
    error: str = "",
) -> str:
    lines = [
        ACK_START,
        f"event_id: {(event_id or '').strip()}",
        f"ok: {'true' if ok else 'false'}",
    ]
    action_norm = (action or "").strip().lower()
    if action_norm in {"add", "remove"}:
        lines.append(f"action: {action_norm}")
    if entry_id.strip():
        lines.append(f"id: {entry_id.strip()}")
    if error.strip():
        # Single-line for protocol safety
        err = error.strip().replace("\n", " ").replace("\r", "")
        lines.append(f"error: {err}")
    lines.append(ACK_END)
    return "\n".join(lines)


def parse_collect_ack(body: str) -> dict[str, str] | None:
    text = (body or "").strip()
    if ACK_START not in text:
        return None
    start = text.find(ACK_START)
    end = text.find(ACK_END, start)
    block = text[start + len(ACK_START) : end if end > start else len(text)].strip()
    if not block:
        return None
    parsed: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed or None


def _auto_summary(text: str, *, fallback: str = "收藏") -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s[:200]
    return fallback[:200]


async def _send_ack(
    room_id: str,
    message: str,
    *,
    client: Any | None = None,
    send_text: SendTextFn | None = None,
) -> None:
    """Deliver ACK: prefer explicit send_text, then remote_turn, then Matrix client."""
    fn = send_text or get_turn_send_text()
    if fn is not None:
        try:
            await fn(room_id, message)
        except Exception as exc:
            logger.warning("collect ack failed via send_text: %s", exc)
        return
    if client is not None:
        try:
            from src.matrix_client.send_guard import matrix_room_send_text

            await matrix_room_send_text(client, room_id, message)
        except Exception as exc:
            logger.warning("collect ack failed via client: %s", exc)
        return
    logger.warning("collect ack dropped: no send path (room=%s)", room_id)


def schedule_handle_collect(
    root: Any,
    room_id: str,
    body: str,
    *,
    client: Any | None = None,
    send_text: SendTextFn | None = None,
) -> None:
    """Fire-and-forget collect handling (prefilter path)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(try_handle_collect(root, room_id, body, client=client, send_text=send_text))
    _ACK_TASKS.add(task)
    task.add_done_callback(_ACK_TASKS.discard)


async def try_handle_collect(
    root: Any,
    room_id: str,
    body: str,
    *,
    client: Any | None = None,
    send_text: SendTextFn | None = None,
) -> bool:
    """Consume a collect envelope. Returns True when handled (do not schedule agent)."""
    if not is_collect_message(body):
        return False

    parsed = parse_collect(body)
    if parsed is None:
        await _send_ack(
            room_id,
            build_collect_ack(event_id="", ok=False, error="收藏请求无效"),
            client=client,
            send_text=send_text,
        )
        return True

    event_id = (parsed.get("event_id") or "").strip()
    action = parsed.get("action", "add")

    from src.records.facade import facade_from_root

    facade = facade_from_root(root)

    if action == "remove":
        result = await _remove_collect(facade, parsed)
    else:
        result = await _add_collect(facade, parsed, client=client)

    await _send_ack(
        room_id,
        build_collect_ack(
            event_id=event_id,
            ok=result.ok,
            action=action,
            entry_id=str((result.metadata or {}).get("id") or ""),
            error="" if result.ok else result.message,
        ),
        client=client,
        send_text=send_text,
    )
    return True


async def _remove_collect(facade: Any, parsed: dict[str, str]) -> Any:
    from src.records.facade import FacadeResult

    entry_id = (parsed.get("id") or "").strip()
    event_id = (parsed.get("event_id") or "").strip()
    if entry_id:
        return await facade.remove(entry_id, origin="user")
    if event_id and facade.store is not None and facade.store.user is not None:
        existing = await facade.store.user.find_by_url(matrix_event_source_url(event_id))
        if existing:
            return await facade.remove(existing.id, origin="user")
    return FacadeResult(False, "未找到对应收藏")


async def _add_collect(facade: Any, parsed: dict[str, str], *, client: Any | None) -> Any:
    from src.records.facade import FacadeResult

    event_id = (parsed.get("event_id") or "").strip()
    msgtype = (parsed.get("msgtype") or "m.text").strip()
    title = (parsed.get("title") or "").strip()
    body_text = (parsed.get("body") or "").strip()
    mxc = (parsed.get("mxc") or "").strip()
    filename = (parsed.get("filename") or "").strip()
    mime = (parsed.get("mime") or "").strip()
    source_url = matrix_event_source_url(event_id)

    is_media = msgtype in {"m.file", "m.image"} or bool(mxc)
    if is_media:
        if not mxc.startswith("mxc://"):
            return FacadeResult(False, "文件收藏需要有效 mxc")
        if client is None:
            return FacadeResult(False, "无法下载媒体（无 Matrix 客户端）")
        from src.matrix_client.remote_vision import download_mxc_bytes

        data = await download_mxc_bytes(client, mxc)
        if not data:
            return FacadeResult(False, "媒体下载失败")
        fname = filename or Path(mxc).name or "file.bin"
        if not title:
            title = fname
        summary = _auto_summary(body_text, fallback=f"收藏文件：{fname}")
        return await facade.add_user_file(
            title=title,
            summary=summary,
            filename=fname,
            file_bytes=data,
            mime=mime,
            source_url=source_url,
            note=f"mxc: {mxc}",
            tags=["matrix", "mobile"],
        )

    if not body_text and not title:
        return FacadeResult(False, "文本收藏需要 body 或 title")
    if not title:
        title = _auto_summary(body_text, fallback="聊天收藏")[:80]
    summary = _auto_summary(body_text, fallback=title)
    return await facade.add_user(
        title=title,
        summary=summary,
        url=source_url,
        content=body_text or title,
        tags=["matrix", "mobile"],
    )

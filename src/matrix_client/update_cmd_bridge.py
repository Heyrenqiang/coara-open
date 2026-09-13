"""Matrix side-channel: 手机消息中心指令 → 收件箱操作。

App sends ``[COARA_UPDATE_CMD]`` (read / archive / mark_read / review)；
PC 执行后回 ``[COARA_UPDATE_ACK]`` 并推送刷新后的 ``[COARA_UPDATES]`` 负载。
与回合无关，走 prefilter 侧信道（同 collect 模式）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from src.core.logger import logger

CMD_START = "[COARA_UPDATE_CMD]"
CMD_END = "[/COARA_UPDATE_CMD]"
ACK_START = "[COARA_UPDATE_ACK]"
ACK_END = "[/COARA_UPDATE_ACK]"

_CMD_TASKS: set[asyncio.Task[Any]] = set()


def is_update_cmd_message(body: str) -> bool:
    return CMD_START in (body or "")


def parse_update_cmd(body: str) -> dict[str, Any] | None:
    text = (body or "").strip()
    start = text.find(CMD_START)
    if start < 0:
        return None
    end = text.find(CMD_END, start)
    block = text[start + len(CMD_START) : end if end > start else len(text)].strip()
    if not block:
        return None
    try:
        data = json.loads(block)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def format_update_ack(payload: dict[str, Any]) -> str:
    return f"{ACK_START}\n{json.dumps(payload, ensure_ascii=False)}\n{ACK_END}"


def schedule_handle_update_cmd(root: Any, room_id: str, body: str) -> None:
    task = asyncio.create_task(_handle(root, room_id, body))
    _CMD_TASKS.add(task)
    task.add_done_callback(_on_cmd_task_done)


def _on_cmd_task_done(task: asyncio.Task[Any]) -> None:
    """收尾登记 + 取异常：只 discard 不取 exception 会让 ACK 失败静默丢失。"""
    _CMD_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"update cmd handler failed: {exc!r}")


async def _handle(root: Any, room_id: str, body: str) -> None:
    data = parse_update_cmd(body)
    ack: dict[str, Any] = {"ok": False}
    if data is not None:
        ack = await _execute(root, data)
    await _send_ack(root, room_id, ack)
    # 推刷新后的负载（含最新消息列表），手机端据此更新抽屉
    push = getattr(root, "_push_workspace_updates_state_to_matrix", None)
    if push is not None:
        try:
            await push()
        except Exception as exc:
            logger.debug(f"updates push after cmd skipped: {exc}")


async def _execute(root: Any, data: dict[str, Any]) -> dict[str, Any]:
    action = str(data.get("action") or "").strip()
    message_id = str(data.get("message_id") or "").strip()
    store = root._updates_store() if hasattr(root, "_updates_store") else None
    if store is None:
        return {"ok": False, "action": action, "error": "updates store unavailable"}

    if action in {"read", "archive"}:
        if not message_id:
            return {"ok": False, "action": action, "error": "missing message_id"}
        msg = store.mark_read(message_id) if action == "read" else store.archive(message_id)
        return {"ok": msg is not None, "action": action, "message_id": message_id}

    if action == "mark_read":
        workspace = str(data.get("workspace") or "").strip()
        if not workspace:
            return {"ok": False, "action": action, "error": "missing workspace"}
        store.mark_all_read(workspace)
        return {"ok": True, "action": action, "workspace": workspace}

    if action == "review":
        text = str(data.get("text") or "").strip()
        from src.workspace.updates.review import ReviewError, review_workspace_update

        try:
            result = await review_workspace_update(root, message_id=message_id, text=text)
        except ReviewError as exc:
            return {"ok": False, "action": action, "message_id": message_id, "error": str(exc)}
        return {"ok": True, "action": action, "message_id": message_id, **result}

    return {"ok": False, "action": action, "error": "unknown action"}


async def _send_ack(root: Any, room_id: str, payload: dict[str, Any]) -> None:
    notify = getattr(root, "matrix_notify", None)
    if notify is None:
        return
    try:
        await notify.send_to_user(format_update_ack(payload))
    except Exception as exc:
        logger.debug(f"update cmd ack skipped: {exc}")

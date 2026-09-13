"""Matrix side-channel for vault unlock (password never enters LLM context)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from src.coara.turn_context import get_turn_send_text
from src.core.logger import logger
from src.matrix_client.pending_interactions import (
    KIND_VAULT,
    discard_pending_interaction,
    record_pending_interaction,
)
from src.vault.prompt_registry import clear as clear_pending
from src.vault.prompt_registry import resolve, set_pending

_VAULT_START = "[COARA_VAULT]"
_VAULT_END = "[/COARA_VAULT]"
_REPLY_START = "[COARA_VAULT_REPLY]"
_REPLY_END = "[/COARA_VAULT_REPLY]"


def format_vault_prompt(*, initialized: bool, locked: bool, title: str = "", question: str = "", hint: str = "") -> str:
    lines = [
        _VAULT_START,
        f"initialized: {'true' if initialized else 'false'}",
        f"locked: {'true' if locked else 'false'}",
    ]
    if title.strip():
        lines.append(f"title: {title.strip()}")
    if question.strip():
        lines.append(f"question: {question.strip()}")
    if hint.strip():
        lines.append(f"hint: {hint.strip()}")
    lines.append(_VAULT_END)
    lines.append("")
    if initialized:
        lines.append("保险柜已锁定。请在下方聊天卡片输入主密码解锁（密码不会进入 AI 对话）。")
    else:
        lines.append("保险柜尚未创建。请在下方聊天卡片输入主密码以创建（密码不会进入 AI 对话）。")
    return "\n".join(lines)


def build_vault_reply_payload(*, action: str, password: str = "") -> str:
    action_norm = action.strip().lower()
    lines = [_REPLY_START, f"action: {action_norm}"]
    if action_norm == "unlock" and password:
        lines.append(f"password: {password}")
    lines.append(_REPLY_END)
    return "\n".join(lines)


_VAULT_VALID_ACTIONS = frozenset({"unlock", "status", "cancel"})


def parse_vault_reply(body: str) -> dict[str, str] | None:
    text = body.strip()
    if _REPLY_START not in text:
        return None
    start = text.find(_REPLY_START)
    end = text.find(_REPLY_END, start)
    block = text[start + len(_REPLY_START) : end if end > start else len(text)].strip()
    if not block:
        return None
    parsed: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        parsed[key.strip().lower()] = value.strip()
    action = parsed.get("action", "").lower()
    if action not in _VAULT_VALID_ACTIONS:
        return None
    return parsed


def is_vault_reply_message(body: str) -> bool:
    return _REPLY_START in body


_VAULT_ACK_PREFIX = re.compile(r"^\[vault\]", re.IGNORECASE)

# Tracked fire-and-forget ack tasks so they aren't garbage-collected mid-send.
_VAULT_ACK_TASKS: set[asyncio.Task[Any]] = set()


def is_vault_ack_message(body: str) -> bool:
    return bool(_VAULT_ACK_PREFIX.match(body.strip()))


def _schedule_vault_ack(room_id: str, message: str) -> None:
    send_text = get_turn_send_text()
    if send_text is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(send_text(room_id, message))
    _VAULT_ACK_TASKS.add(task)
    task.add_done_callback(_VAULT_ACK_TASKS.discard)


def try_resolve_vault_reply(root: Any, room_id: str, body: str) -> bool:
    """Consume mobile/CLI vault side-channel replies before agent scheduling."""
    parsed = parse_vault_reply(body)
    if parsed is None:
        return False

    service = getattr(root, "vault_service", None)
    if service is None:
        _schedule_vault_ack(room_id, "[vault] 保险柜未启用。")
        return True

    action = parsed.get("action", "").lower()
    if action == "status":
        status = service.status()
        lock_text = "已锁定" if status.locked else "已解锁"
        init_text = "已初始化" if status.initialized else "未初始化"
        _schedule_vault_ack(room_id, f"[vault] {init_text} · {lock_text} · 封存文件 {status.entries}")
        return True

    if action == "unlock":
        password = parsed.get("password", "")
        if not password:
            _schedule_vault_ack(room_id, "[vault] 缺少密码。")
            # Keep waiting — user can retry from the card
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # 无事件循环（离线/测试场景）走同步路径
            ok, _, message = service.setup_or_unlock_with_feedback(password, persistent=False)
            _schedule_vault_ack(room_id, f"[vault] {message}")
            if ok:
                resolve({"ok": True, "cancelled": False, "message": message})
                discard_pending_interaction(KIND_VAULT, room_id)
            return True
        # rekey（旧 KDF 升级）可能耗时数秒 走异步路径避免卡住事件循环
        task = loop.create_task(_run_vault_unlock(service, room_id, password))
        _VAULT_ACK_TASKS.add(task)
        task.add_done_callback(_VAULT_ACK_TASKS.discard)
        return True

    if action == "cancel":
        _schedule_vault_ack(room_id, "[vault] 已取消。")
        resolve({"ok": False, "cancelled": True, "message": "用户取消。"})
        discard_pending_interaction(KIND_VAULT, room_id)
        return True

    return False


async def _run_vault_unlock(service: Any, room_id: str, password: str) -> None:
    """Event-loop-safe unlock: legacy-KDF rekey runs off-loop inside the service"""
    ok, _, message = await service.setup_or_unlock_with_feedback_async(password, persistent=False)
    _schedule_vault_ack(room_id, f"[vault] {message}")
    if ok:
        resolve({"ok": True, "cancelled": False, "message": message})
        discard_pending_interaction(KIND_VAULT, room_id)
    # Wrong password: keep future open so the tool stays blocked for retry / cancel / timeout


async def maybe_prompt_vault_unlock(root: Any) -> str:
    """Push vault unlock card to Matrix and wait for the user to reply.

    Blocks until the user sends ``[COARA_VAULT_REPLY]`` (consumed by
    :func:`try_resolve_vault_reply`, which resolves the pending future) or
    the timeout expires.

    Returns one of: ``"unlocked"``, ``"cancelled"``, ``"timeout"``, ``"error"``.
    """
    service = getattr(root, "vault_service", None)
    if service is None:
        return "error"
    if service.is_unlocked():
        return "unlocked"
    from src.coara.turn_context import get_turn_channel_id

    room_id = get_turn_channel_id()
    if not room_id:
        return "error"
    send_text = get_turn_send_text()
    if send_text is None:
        return "error"
    initialized = service.is_initialized()
    question = (
        "请输入主密码（不会进入 AI 对话）" if initialized else "请设置主密码（≥8 位，字母数字混合，不会进入 AI 对话）"
    )
    message = format_vault_prompt(
        initialized=initialized,
        locked=True,
        title="保险柜解锁" if initialized else "创建保险柜",
        question=question,
        hint="Agent 需要访问保险柜",
    )
    await send_text(room_id, message)
    # Persist (room, kind, expiry) so a restart can invalidate the dead card;
    # TTL mirrors the wait_for timeout below.
    record_pending_interaction(KIND_VAULT, room_id, ttl_seconds=60.0)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    set_pending(future)
    logger.debug("Vault unlock prompt pushed to Matrix, waiting for reply")
    try:
        result = await asyncio.wait_for(future, timeout=60.0)
        if bool(result.get("ok", False)):
            return "unlocked"
        if result.get("cancelled"):
            return "cancelled"
        return "error"
    except TimeoutError:
        logger.warning("Vault unlock prompt timed out (Matrix)")
        return "timeout"
    except asyncio.CancelledError:
        logger.debug("Vault unlock prompt cancelled (Matrix)")
        return "cancelled"
    finally:
        clear_pending()

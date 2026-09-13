"""Web side-channel for vault unlock (password never enters LLM context).

Mirrors :mod:`src.matrix_client.vault_bridge` but routes the password prompt
through the WebSocket instead of Matrix room messages. The password is sent
as a dedicated ``vault_reply`` WS message type — it is NEVER forwarded to
the agent / LLM. The backend unlocks the vault in-process and replies with
a ``vault_result`` message (success/failure).

Flow:
1. ``VaultTool`` hits a locked vault → calls :func:`prompt_vault_unlock_web_and_wait`
2. Server pushes ``{"type": "vault_prompt", "initialized": ..., "locked": ...,
   "title": ..., "question": ..., "hint": ...}``
3. Browser shows ``VaultPromptCard`` (password input + submit)
4. User submits → browser sends ``{"type": "vault_reply", "password": ...}``
5. :func:`handle_vault_reply` unlocks the vault in-process, resolves the
   pending future, and replies ``{"type": "vault_result", "ok": ..., "message": ...}``
6. Browser dismisses card on success, shows error on failure

The prompt is **synchronous** (await): the tool call that triggered it blocks
until the user submits the password (or the timeout expires). This matches
the CLI modal behavior — the tool returns success once unlocked, rather than
a fire-and-forget "locked" hint that forces the agent to retry (which was
prone to the popup never appearing due to cooldown-throttled retries).

No ``prompt_id`` is used: only one vault prompt can be active at a time
(vault lock is a single global state), so the browser and server simply
react to the latest ``vault_prompt`` / ``vault_result`` pair.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.core.logger import logger
from src.vault.prompt_registry import clear as clear_pending
from src.vault.prompt_registry import resolve, set_pending

if TYPE_CHECKING:
    from src.coara.root import RootCoara
    from src.ui.web_socket_registry import WebSocketRegistry


async def prompt_vault_unlock_web_and_wait(
    root: RootCoara,
    registry: WebSocketRegistry,
    *,
    timeout_seconds: float = 60.0,
) -> str:
    """Push a vault unlock prompt to the browser and wait for the password.

    Blocks until the user submits the password (via ``vault_reply``) or the
    timeout expires. The actual unlock is performed by :func:`handle_vault_reply`
    when the password arrives; this function just waits for the outcome.

    Returns one of: ``"unlocked"``, ``"cancelled"``, ``"timeout"``, ``"error"``.
    """
    service = getattr(root, "vault_service", None)
    if service is None:
        return "error"
    if service.is_unlocked():
        return "unlocked"
    if not registry.has_active():
        return "error"

    initialized = service.is_initialized()
    title = "保险柜解锁" if initialized else "创建保险柜"
    question = "请输入主密码（不会进入 AI 对话）" if initialized else "请设置主密码（≥8 位，不会进入 AI 对话）"
    hint = "Agent 需要访问保险柜。"
    message = {
        "type": "vault_prompt",
        "initialized": initialized,
        "locked": True,
        "title": title,
        "question": question,
        "hint": hint,
    }
    sent = await registry.send_to_active(message)
    if not sent:
        logger.warning("Vault unlock prompt: failed to send to Web client")
        return "error"

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    set_pending(future)
    logger.debug("Vault unlock prompt pushed to Web client, waiting for reply")

    try:
        result = await asyncio.wait_for(future, timeout=timeout_seconds)
        if bool(result.get("ok", False)):
            return "unlocked"
        if result.get("cancelled"):
            return "cancelled"
        return "error"
    except TimeoutError:
        logger.warning(f"Vault unlock prompt timed out after {timeout_seconds}s")
        return "timeout"
    except asyncio.CancelledError:
        logger.debug("Vault unlock prompt cancelled")
        return "cancelled"
    finally:
        clear_pending()


async def handle_vault_reply(
    root: RootCoara,
    password: str,
) -> dict[str, Any]:
    """Process a ``vault_reply`` WS message: unlock the vault.

    Returns a result dict suitable for sending back as ``vault_result``.
    The password is consumed here and NEVER forwarded to the agent.
    If a pending vault prompt future exists, it is resolved with the result
    so :func:`prompt_vault_unlock_web_and_wait` unblocks.
    """
    service = getattr(root, "vault_service", None)
    if service is None:
        result = {"ok": False, "message": "保险柜未启用。", "created": False}
    elif not password:
        result = {"ok": False, "message": "密码不能为空。", "created": False}
    else:
        ok, created, message = await service.setup_or_unlock_with_feedback_async(password, persistent=False)
        result = {"ok": ok, "message": message, "created": created}

    # Only settle the waiter on success. Wrong password keeps the modal + tool
    # blocked so the user can retry (cancel / timeout still release the future).
    if bool(result.get("ok")):
        resolve(result)

    return result


def handle_vault_cancel(root: RootCoara) -> dict[str, Any]:
    """Process a ``vault_cancel`` WS message: user dismissed the prompt.

    Resolves the pending vault prompt future with a failure result so the
    blocked tool call returns immediately instead of waiting for the timeout.
    """
    result = {"ok": False, "message": "用户取消。", "created": False, "cancelled": True}
    resolve(result)
    return result

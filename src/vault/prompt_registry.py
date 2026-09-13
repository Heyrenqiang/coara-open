"""Shared registry for the pending vault password prompt future.

The vault unlock prompt is a single global state (only one vault, only one
lock). When a tool call blocks waiting for the password, the pending future
is registered here. Any frontend's side-channel (Web ``vault_reply`` /
Matrix ``[COARA_VAULT_REPLY]``) resolves the *same* future via this module,
so cross-frontend unlock wakes the waiting tool call regardless of which
frontend the user replies from.

Previously each bridge kept its own module-level ``_pending_vault_future``,
which meant: Web turn blocks → Matrix user sends password → vault unlocked
in-process, but the Web future never resolved (it lived in a different
module) → the Web tool call waited the full 60s timeout and reported a
misleading "timeout" error despite the vault being unlocked.
"""

from __future__ import annotations

import asyncio
from typing import Any

_pending_future: asyncio.Future[dict[str, Any]] | None = None


def set_pending(future: asyncio.Future[dict[str, Any]]) -> None:
    """Register the pending vault prompt future.

    Overwrites any previously pending future (the old one is cancelled —
    a new prompt means the previous one was abandoned).
    """
    global _pending_future
    if _pending_future is not None and not _pending_future.done():
        _pending_future.cancel()
    _pending_future = future


def resolve(result: dict[str, Any]) -> bool:
    """Resolve the pending future with ``result`` if it exists and isn't done.

    Returns True if a pending future was resolved.
    """
    global _pending_future
    if _pending_future is not None and not _pending_future.done():
        _pending_future.set_result(result)
        return True
    return False


def clear() -> None:
    """Clear the pending future (called by the waiter's finally block)."""
    global _pending_future
    _pending_future = None


def has_pending() -> bool:
    """Whether a pending vault prompt future exists and is unresolved."""
    return _pending_future is not None and not _pending_future.done()

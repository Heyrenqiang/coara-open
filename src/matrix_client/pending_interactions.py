"""Persist Matrix pending-interaction metadata across process restarts.

Vault unlock cards push a prompt to a Matrix room and keep the waiting future
in process memory. When the process restarts (graceful or not), the card on
the phone goes dead silently — tapping it does nothing and the user gets no
feedback. This module records ``(room, kind, expiry)`` to a small JSON file
(atomic writes via ``src.core.json_store``) so the next startup can push a
one-shot invalidation notice to the room and clear the record.

Approval cards do not use this store: their future lives in
:class:`~src.coara.approval_center.ApprovalCenter`, whose settle frames
(``m.coara.approval_resolved``) close the phone card after restart. Stale
``approval`` records left by older processes fall back to the generic notice.

Lifecycle:

- ``record_pending_interaction`` — a bridge has just sent a card and is now
  waiting on an in-memory future
- ``discard_pending_interaction`` — the user actually resolved the card
  (reply consumed); timeouts / cancels keep the record so a restart still
  notifies, and the expiry bounds how long a stale record can linger
- ``notify_stale_pending_interactions`` — startup sweep after login: unexpired
  records get a "service restarted, request invalidated" text via the normal
  room send channel, then the file is cleared regardless (never notify twice)
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.json_store import write_json_atomic
from src.core.logger import logger

KIND_VAULT = "vault"

# User-visible restart invalidation notices (runtime strings keep punctuation).
_STALE_NOTICE_BY_KIND = {
    KIND_VAULT: "coara 服务已重启，此前的保险柜解锁请求已失效，请重新发起。",
}
_STALE_NOTICE_GENERIC = "coara 服务已重启，此前的待处理请求已失效，请重新发起。"


@dataclass(slots=True)
class PendingInteractionRecord:
    kind: str
    room_id: str
    created_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "room_id": self.room_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


# In-memory mirror of the on-disk file; keyed by (kind, room_id) — each bridge
# holds at most one pending prompt per room per kind (per-room locks).
_RECORDS: dict[tuple[str, str], PendingInteractionRecord] = {}

# Test / embedder override; production resolves lazily from config_manager.
_configured_store_path: Path | None = None


def configure_pending_interactions_store(path: Path | None) -> None:
    """Override the store file location (tests; production uses the default)."""
    global _configured_store_path
    _configured_store_path = path


def pending_interactions_store_path() -> Path | None:
    """Resolve the store file: override → ``<coara_home>/runtime/``."""
    if _configured_store_path is not None:
        return _configured_store_path
    try:
        from src.core.config import config_manager

        config = config_manager._config  # noqa: SLF001
        home = getattr(config, "coara_home", None) if config is not None else None
        if home is None:
            return None
        return Path(home) / "runtime" / "matrix_pending_interactions.json"
    except Exception:
        return None


def _persist() -> None:
    path = pending_interactions_store_path()
    if path is None:
        return
    payload = {
        "version": 1,
        "pending": [record.to_dict() for record in _RECORDS.values()],
    }
    try:
        write_json_atomic(path, payload)
    except Exception as exc:
        logger.warning(f"Failed to persist Matrix pending interactions: {exc}")


def record_pending_interaction(kind: str, room_id: str, *, ttl_seconds: float) -> None:
    """Note that a card was sent to *room_id* and is now waiting on a future."""
    if not room_id:
        return
    now = time.time()
    _RECORDS[(kind, room_id)] = PendingInteractionRecord(
        kind=kind,
        room_id=room_id,
        created_at=now,
        expires_at=now + max(ttl_seconds, 0.0),
    )
    _persist()


def discard_pending_interaction(kind: str, room_id: str) -> None:
    """Drop the record once the user has actually resolved the card."""
    if (kind, room_id) in _RECORDS:
        _RECORDS.pop((kind, room_id), None)
        _persist()


def load_pending_interactions(path: Path | None = None) -> list[PendingInteractionRecord]:
    """Read persisted records (empty list when missing / corrupt)."""
    store = path if path is not None else pending_interactions_store_path()
    if store is None or not store.exists():
        return []
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Failed to load Matrix pending interactions from {store}: {exc}")
        return []
    items = raw.get("pending") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    records: list[PendingInteractionRecord] = []
    for item in items:
        try:
            records.append(
                PendingInteractionRecord(
                    kind=str(item["kind"]),
                    room_id=str(item["room_id"]),
                    created_at=float(item["created_at"]),
                    expires_at=float(item["expires_at"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(f"Skipping invalid pending interaction record: {exc}")
    return records


async def notify_stale_pending_interactions(
    send_text: Callable[[str, str], Awaitable[Any]],
    *,
    now: float | None = None,
) -> int:
    """Push a restart invalidation notice for each unexpired pending record.

    Runs once at Matrix client startup (after login + invite catch-up) with the
    normal room send channel. The file is cleared afterwards regardless of
    delivery outcome — notices are one-shot and must never repeat.
    Returns the number of notices successfully sent.
    """
    path = pending_interactions_store_path()
    records = load_pending_interactions(path)
    if not records:
        return 0
    current = time.time() if now is None else now
    notified = 0
    for record in records:
        if record.expires_at < current:
            continue
        text = _STALE_NOTICE_BY_KIND.get(record.kind, _STALE_NOTICE_GENERIC)
        try:
            sent = await send_text(record.room_id, text)
        except Exception as exc:
            logger.warning(f"Failed to send stale-interaction notice to {record.room_id}: {exc}")
            continue
        if sent is not False:
            notified += 1
    _RECORDS.clear()
    if path is not None:
        with contextlib.suppress(Exception):
            path.unlink(missing_ok=True)
    if notified:
        logger.info(f"Notified {notified} stale Matrix pending interaction(s) after restart")
    return notified

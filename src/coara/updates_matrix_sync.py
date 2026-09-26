"""Matrix hidden payloads for workspace updates sync (mobile app badges)."""

from __future__ import annotations

import json
from typing import Any

UPDATES_START = "[COARA_UPDATES]"
UPDATES_END = "[/COARA_UPDATES]"


def format_updates_payload(
    *,
    payload_type: str,
    workspaces: list[dict[str, Any]],
    message: dict[str, Any] | None = None,
    pending: list[dict[str, Any]] | None = None,
) -> str:
    body: dict[str, Any] = {"type": payload_type, "workspaces": workspaces}
    if message is not None:
        body["message"] = message
    if pending is not None:
        body["pending"] = pending
    return f"{UPDATES_START}\n{json.dumps(body, ensure_ascii=False)}\n{UPDATES_END}"


def is_updates_control_message(body: str) -> bool:
    return UPDATES_START in body

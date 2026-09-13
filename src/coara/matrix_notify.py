"""Push coara text replies to a Matrix room (remote outbound adapter).

Auto-run replies (background / inbound event) are sent as **plain assistant text** —
no ``考拉 […]`` banner. 内核化后三端独立：CLI 经 RootShim 事件通道镜像，无服务端同步。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.logger import logger

# Return True/False when known; legacy callbacks may return None (= success).
SendTextFn = Callable[[str, str], Awaitable[Any]]

_NOTIFY_ROOM_FILE = "matrix_notify_room_id.txt"


def notify_room_file(coara_home: Path | str) -> Path:
    return Path(coara_home).expanduser().resolve() / _NOTIFY_ROOM_FILE


@dataclass
class MatrixNotificationBridge:
    """Registered by the CLI Matrix sync task; used for external event reports."""

    default_room_id: str | None = None
    last_remote_room_id: str | None = None
    send_text: SendTextFn | None = field(default=None, repr=False)

    def register_send(self, send_text: SendTextFn, *, default_room_id: str | None = None) -> None:
        self.send_text = send_text
        if default_room_id:
            self.default_room_id = default_room_id

    def load_persisted_room(self, coara_home: Path | str | None) -> str | None:
        """Load last known remote room from coara Home (survives CLI restarts)."""
        if not coara_home:
            return None
        path = notify_room_file(coara_home)
        if not path.is_file():
            return None
        # Size guard: the file holds a single room id (tiny). Refuse absurd
        # files instead of reading unbounded data into memory.
        if path.stat().st_size > 1024 * 1024:
            logger.warning(f"[Matrix] Notify room file too large, ignoring: {path}")
            return None
        room_id = path.read_text(encoding="utf-8").strip()
        if room_id:
            self.last_remote_room_id = room_id
            logger.debug(f"[Matrix] Loaded notify room from {path}")
        return room_id or None

    def remember_remote_room(self, room_id: str, *, coara_home: Path | str | None = None) -> None:
        if not room_id:
            return
        self.last_remote_room_id = room_id
        if coara_home:
            path = notify_room_file(coara_home)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(room_id, encoding="utf-8")
            except OSError as exc:
                logger.warning(f"[Matrix] Failed to persist notify room: {exc}")

    def clear_persisted_room(self, coara_home: Path | str | None = None) -> None:
        """Drop stale notify room (e.g. after homeserver migration)."""
        self.last_remote_room_id = None
        self.default_room_id = None
        if not coara_home:
            return
        path = notify_room_file(coara_home)
        try:
            if path.is_file():
                path.unlink()
                logger.info(f"[Matrix] Cleared stale notify room file {path}")
        except OSError as exc:
            logger.warning(f"[Matrix] Failed to clear notify room file: {exc}")

    def resolve_room_id(self) -> str | None:
        return self.last_remote_room_id or self.default_room_id

    async def send_to_user(self, body: str) -> bool:
        room_id = self.resolve_room_id()
        if not room_id or not self.send_text:
            return False
        text = body.strip()
        if not text:
            return False
        try:
            result = await self.send_text(room_id, text)
            if result is False:
                logger.warning(f"[Matrix] Message not delivered to {room_id} ({len(text)} chars)")
                return False
            logger.info(f"[Matrix] Message sent to {room_id} ({len(text)} chars)")
            return True
        except Exception as exc:
            logger.warning(f"[Matrix] Failed to send message: {exc}")
            return False

    @staticmethod
    def format_event_response(_source_id: str, response: str) -> str:
        """Plain text for phone; CLI labels come from remote_sync, not this prefix."""
        return response.strip()

"""Safe Matrix room_send helpers for shutdown races."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from src.core.logger import logger

DEFAULT_SEND_MAX_ATTEMPTS = 3
DEFAULT_SEND_RETRY_DELAY_S = 0.35

_MD_CODE_BLOCK_RE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"\*(.+?)\*")


def is_matrix_client_logged_in(client: Any) -> bool:
    """Return True when nio still has a session token (not yet logged out)."""
    try:
        return bool(client.logged_in)
    except AttributeError:
        return bool(getattr(client, "access_token", None))


def simple_md_to_html(text: str) -> str:
    text = _MD_CODE_BLOCK_RE.sub(r"<pre><code>\2</code></pre>", text)
    text = _MD_INLINE_CODE_RE.sub(r"<code>\1</code>", text)
    text = _MD_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC_RE.sub(r"<em>\1</em>", text)
    return text.replace("\n", "<br/>")


def _error_http_status(result: Any) -> int | None:
    raw = getattr(result, "status_code", None)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _error_is_forbidden(result: Any) -> bool:
    raw = getattr(result, "status_code", None)
    if raw == 403 or raw == "403":
        return True
    if isinstance(raw, str) and raw == "M_FORBIDDEN":
        text = str(getattr(result, "message", result)).lower()
        # SQLite writer lock is mislabeled M_FORBIDDEN by GoMatrix — retry instead.
        return "database is locked" not in text and "locked (517" not in text
    text = str(getattr(result, "message", result)).lower()
    if "m_forbidden" in text and ("database is locked" in text or "locked (517" in text):
        return False
    return "m_forbidden" in text or "not joined" in text or "user not joined" in text


def _is_transient_send_error(*, exc: BaseException | None = None, result: Any = None) -> bool:
    if exc is not None:
        text = str(exc).lower()
        return (
            "database is locked" in text
            or "locked (517" in text
            or "timeout" in text
            or "connection" in text
            or "temporar" in text
        )
    from nio.responses import ErrorResponse

    if isinstance(result, ErrorResponse):
        text = str(result).lower()
        if "database is locked" in text or "locked (517" in text:
            return True
        status_code = _error_http_status(result)
        if status_code in {408, 429, 500, 502, 503, 504}:
            return True
        return "timeout" in text or "connection" in text or "temporar" in text
    return False


def _send_failure_retryable(*, exc: BaseException | None = None, result: Any = None) -> bool:
    if _is_transient_send_error(exc=exc, result=result):
        return True
    if exc is not None:
        text = str(exc).lower()
        return "database is locked" in text
    from nio.responses import ErrorResponse

    if isinstance(result, ErrorResponse):
        if _error_is_forbidden(result):
            return False
        message = str(result).lower()
        status_code = _error_http_status(result)
        if status_code in {408, 429, 500, 502, 503, 504}:
            return True
        return (
            "database is locked" in message or "timeout" in message or "connection" in message or "temporar" in message
        )
    return False


def _is_successful_room_send(result: Any) -> bool:
    from nio import RoomSendResponse

    return isinstance(result, RoomSendResponse)


def _build_text_content(body: str) -> dict[str, Any]:
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    # turn 输出标志（content 自定义键，不进正文）：手机端 typing 指示灯准绳——
    # 仅 final 灭灯，intermediate 继续亮；[COARA_TURN] 结束信封即 final 载体
    content["coara_turn"] = "final" if body.startswith("[COARA_TURN]") else "intermediate"
    if any(c in body for c in ("**", "```", "`")):
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = simple_md_to_html(body)
    return content


async def matrix_room_send_text(
    client: Any,
    room_id: str,
    body: str,
    *,
    log: logging.Logger | None = None,
    max_attempts: int = DEFAULT_SEND_MAX_ATTEMPTS,
    ensure_joined: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Send m.text with retries; return False when skipped or all attempts fail."""
    sink = log or logger
    if not is_matrix_client_logged_in(client):
        sink.debug("[Matrix] skip room_send — client already logged out")
        return False

    content = _build_text_content(body)
    # One txn id per logical send, reused across all retry attempts: the
    # server dedupes on it (PUT .../send/{txnId}), so a retry after a lost
    # response cannot post the message twice.
    tx_id = uuid.uuid4().hex
    last_failure: str | None = None
    rejoined = False
    for attempt in range(1, max_attempts + 1):
        try:
            result = await client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
                tx_id=tx_id,
            )
            if _is_successful_room_send(result):
                return True
            last_failure = repr(result)
            if not rejoined and ensure_joined is not None and _error_is_forbidden(result):
                rejoined = await ensure_joined()
                if rejoined:
                    sink.debug("[Matrix] re-joined %s after forbidden send; retrying", room_id)
                    continue
            if attempt < max_attempts and _send_failure_retryable(result=result):
                sink.warning(
                    "[Matrix] room_send rejected (attempt %d/%d) room=%s: %s",
                    attempt,
                    max_attempts,
                    room_id,
                    result,
                )
                await asyncio.sleep(DEFAULT_SEND_RETRY_DELAY_S * attempt)
                continue
            sink.warning("[Matrix] room_send rejected for %s: %s", room_id, result)
            return False
        except Exception as exc:
            exc_name = type(exc).__name__
            if exc_name == "LocalProtocolError" and "Not logged in" in str(exc):
                sink.debug("[Matrix] room_send skipped — not logged in")
                return False
            last_failure = str(exc)
            if attempt < max_attempts and _send_failure_retryable(exc=exc):
                sink.warning(
                    "[Matrix] room_send retry %d/%d for %s: %s",
                    attempt,
                    max_attempts,
                    room_id,
                    exc,
                )
                await asyncio.sleep(DEFAULT_SEND_RETRY_DELAY_S * attempt)
                continue
            sink.warning("[Matrix] room_send failed for %s: %s", room_id, exc)
            return False

    if last_failure is not None:
        sink.warning("[Matrix] room_send failed for %s after retries: %s", room_id, last_failure)
    return False


async def matrix_room_send_content(
    client: Any,
    room_id: str,
    content: dict[str, Any],
    *,
    log: logging.Logger | None = None,
    max_attempts: int = DEFAULT_SEND_MAX_ATTEMPTS,
    ensure_joined: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Send m.room.message with arbitrary content (files, images)."""
    sink = log or logger
    if not is_matrix_client_logged_in(client):
        sink.debug("[Matrix] skip room_send — client already logged out")
        return False

    # See matrix_room_send_text: one txn id reused across retries for idempotency.
    tx_id = uuid.uuid4().hex
    last_failure: str | None = None
    rejoined = False
    for attempt in range(1, max_attempts + 1):
        try:
            result = await client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
                tx_id=tx_id,
            )
            if _is_successful_room_send(result):
                return True
            last_failure = repr(result)
            if not rejoined and ensure_joined is not None and _error_is_forbidden(result):
                rejoined = await ensure_joined()
                if rejoined:
                    sink.debug("[Matrix] re-joined %s after forbidden send; retrying", room_id)
                    continue
            if attempt < max_attempts and _send_failure_retryable(result=result):
                await asyncio.sleep(DEFAULT_SEND_RETRY_DELAY_S * attempt)
                continue
            sink.warning("[Matrix] room_send rejected for %s: %s", room_id, result)
            return False
        except Exception as exc:
            exc_name = type(exc).__name__
            if exc_name == "LocalProtocolError" and "Not logged in" in str(exc):
                sink.debug("[Matrix] room_send skipped — not logged in")
                return False
            last_failure = str(exc)
            if attempt < max_attempts and _send_failure_retryable(exc=exc):
                await asyncio.sleep(DEFAULT_SEND_RETRY_DELAY_S * attempt)
                continue
            sink.warning("[Matrix] room_send failed for %s: %s", room_id, exc)
            return False

    if last_failure is not None:
        sink.warning("[Matrix] room_send failed for %s after retries: %s", room_id, last_failure)
    return False

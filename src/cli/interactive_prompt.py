"""Interactive UI primitives for the CLI layer.

All terminal rendering lives here so that src/tools/ never imports
questionary directly.

Uses the modal delegate architecture:
interactive prompts render inside the existing PromptSession
instead of creating a second Application.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.cli.modal import PasswordModalDelegate, SelectModalDelegate

_modal_spinner: Any | None = None
_pending_echo: str | None = None


def set_modal_spinner(spinner: Any | None) -> None:
    """Register the active BackgroundSpinner so that modals can attach/detach."""
    global _modal_spinner
    _modal_spinner = spinner


def _get_buffer_text() -> str:
    if _modal_spinner is None or _modal_spinner._session is None:
        return ""
    return _modal_spinner._session.default_buffer.text


def _format_selection_echo(question: str, result: dict[str, Any] | None) -> str:
    if result is None:
        return f"{question} (cancelled)"
    if "selection" in result:
        return f"{question} → {result['selection']}"
    if "free_text" in result:
        text = str(result["free_text"] or "").strip()
        return f"{question} → {text}" if text else f"{question} → (跳过)"
    return question


def _queue_interaction_echo(line: str) -> None:
    global _pending_echo
    _pending_echo = line


def flush_interaction_echo(*, as_chunk: bool = True) -> str | None:
    """Return deferred approval reply line (after tool summary)."""
    global _pending_echo
    if _pending_echo is None:
        return None
    line = _pending_echo
    _pending_echo = None
    if as_chunk and not line.endswith("\n"):
        return line + "\n"
    return line


def _echo_selection(question: str, result: dict[str, Any] | None) -> None:
    _queue_interaction_echo(_format_selection_echo(question, result))


async def prompt_select(
    question: str,
    options: list[dict[str, Any]],
    *,
    allow_free_text: bool = False,
    free_text_label: str = "Other",
    preview: Any | None = None,
    timeout: float | None = None,
) -> dict[str, Any] | None:
    """Show a modal select prompt inside the existing PromptSession.

    Returns ``{"selection": label}``, ``{"free_text": text}``, or ``None`` if cancelled.
    Raises ``TimeoutError`` if ``timeout`` seconds elapse without a response.
    """
    if _modal_spinner is None:
        raise RuntimeError("Modal spinner not registered. Call set_modal_spinner() first.")

    delegate = SelectModalDelegate(
        question=question,
        options=options,
        allow_free_text=allow_free_text,
        free_text_label=free_text_label,
        get_buffer_text=_get_buffer_text,
        preview=preview,
    )
    _modal_spinner.attach_modal(delegate)
    try:
        result = await asyncio.wait_for(delegate.wait_for_result(), timeout=timeout)
    except TimeoutError:
        if not delegate._future.done():
            delegate._cancel()
        raise
    finally:
        _modal_spinner.detach_modal(delegate)
    _echo_selection(question, result)
    return result


async def prompt_password(
    question: str = "保险柜解锁",
    *,
    hint: str = "请输入主密码（不会进入 AI 对话）",
    timeout: float = 300.0,
) -> str | None:
    """Modal password prompt (approval-style panel; echo never shows the password).

    ``timeout`` 与服务端 AttachRemoteInteractionChannel 默认 300s 对齐：
    超时自动取消 modal 并回显「已超时」，返回 None（调用方按未解锁处理）。
    """
    if _modal_spinner is None:
        raise RuntimeError("Modal spinner not registered. Call set_modal_spinner() first.")

    delegate = PasswordModalDelegate(question=question, hint=hint)
    delegate.set_buffer_getter(_get_buffer_text)
    _modal_spinner.attach_modal(delegate)
    try:
        result = await asyncio.wait_for(delegate.wait_for_result(), timeout=timeout)
    except TimeoutError:
        if not delegate._future.done():
            delegate._cancel()
        _queue_interaction_echo(f"{question} → (已超时)")
        return None
    finally:
        _modal_spinner.detach_modal(delegate)
    # Never echo the password into scrollback.
    _queue_interaction_echo(f"{question} → {'已输入' if result else '(取消)'}")
    return result

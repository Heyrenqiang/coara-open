"""Modal delegate system for the Coara CLI.

Modal delegate architecture.
All interactive modals render inside the existing PromptSession,
replacing the input buffer, without a second Application.
"""

from __future__ import annotations

import asyncio
from typing import Any

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText
from prompt_toolkit.key_binding import KeyPressEvent
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

_console_cache: Console | None = None
_console_buf: Any = None


def _render_to_ansi(renderable: Any, *, columns: int) -> str:
    """Render a Rich renderable to an ANSI string for prompt_toolkit integration."""
    from io import StringIO

    global _console_cache, _console_buf
    width = max(20, columns)

    if _console_cache is None or _console_cache.width != width:
        _console_buf = StringIO()
        _console_cache = Console(file=_console_buf, force_terminal=True, width=width, highlight=False)

    _console_buf.seek(0)
    _console_buf.truncate(0)
    _console_cache.print(renderable, end="")
    return _console_buf.getvalue()


class SelectModalDelegate:
    """Modal delegate for single-choice questions with optional free-text input."""

    modal_priority = 10

    def __init__(
        self,
        question: str,
        options: list[dict[str, Any]],
        *,
        allow_free_text: bool = False,
        free_text_label: str = "Other",
        get_buffer_text: Any | None = None,
        preview: Any | None = None,
    ) -> None:
        self.question = question
        self.options = list(options)
        self.allow_free_text = allow_free_text
        self.free_text_label = free_text_label
        self._get_buffer_text = get_buffer_text
        self._preview = preview

        self._selected_index = 0
        self._done = False
        self._cancelled = False
        self._awaiting_free_text = False
        self._future_lazy: asyncio.Future[dict[str, Any] | None] | None = None

        if self.allow_free_text and not any(opt.get("label") == self.free_text_label for opt in self.options):
            self.options.append(
                {
                    "label": self.free_text_label,
                    "description": "输入自定义内容；留空 Enter 跳过",
                }
            )

    @property
    def _future(self) -> asyncio.Future[dict[str, Any] | None]:
        """Lazily created on first use so construction works outside a running loop."""
        if self._future_lazy is None:
            self._future_lazy = asyncio.get_running_loop().create_future()
        return self._future_lazy

    def render_modal_body(self, columns: int) -> AnyFormattedText:
        lines: list[Any] = []
        lines.append(Text.from_markup(f"[bold]{self.question}[/bold]"))
        lines.append(Text(""))
        if self._preview is not None:
            lines.append(self._preview)
            lines.append(Text(""))

        inline_text = self._get_buffer_text() if self._awaiting_free_text and self._get_buffer_text is not None else ""

        for i, opt in enumerate(self.options):
            num = i + 1
            label = opt.get("label", "")
            desc = opt.get("description", "")
            is_last = i == len(self.options) - 1

            if i == self._selected_index:
                if self._awaiting_free_text and is_last:
                    display = inline_text or ""
                    line = Text.from_markup(f"[cyan]→ [{num}] {label}: {display}█[/cyan]")
                else:
                    line = Text.from_markup(f"[cyan]→ [{num}] {label}[/cyan]")
            else:
                line = Text.from_markup(f"[grey50]  [{num}] {label}[/grey50]")

            lines.append(line)
            if desc and not (self._awaiting_free_text and i == self._selected_index and is_last):
                lines.append(Text(f"      {desc}", style="dim"))

        if self._awaiting_free_text:
            lines.append(Text(""))
            lines.append(Text("  输入后 Enter 提交；留空 Enter 跳过。", style="dim italic"))

        panel = Panel(
            Group(*lines),
            border_style="grey50",
            title="[bold]question[/bold]",
            title_align="left",
            padding=(0, 1),
        )
        ansi = _render_to_ansi(panel, columns=columns)
        return ANSI(ansi.rstrip("\n"))

    def modal_hides_input_buffer(self) -> bool:
        return not self._awaiting_free_text

    def modal_allows_text_input(self) -> bool:
        return self._awaiting_free_text

    def should_handle_modal_key(self, key: str) -> bool:
        if self._done or self._cancelled:
            return False
        if self._awaiting_free_text:
            return key in {"enter", "escape", "c-c", "up", "down"}
        return key in {
            "up",
            "down",
            "enter",
            "escape",
            "c-c",
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
        }

    def handle_modal_key(self, key: str, event: KeyPressEvent) -> None:
        if self._done or self._cancelled:
            return

        if self._awaiting_free_text:
            if key == "enter":
                text = event.current_buffer.text.strip()
                self._result = {"free_text": text}
                self._done = True
                self._clear_buffer(event.current_buffer)
                self._future.set_result(self._result)
            elif key in ("escape", "c-c"):
                self._cancel()
                self._clear_buffer(event.current_buffer)
            elif key in ("up", "down"):
                self._awaiting_free_text = False
                self._clear_buffer(event.current_buffer)
                if key == "up":
                    self._selected_index = (self._selected_index - 1) % len(self.options)
                else:
                    self._selected_index = (self._selected_index + 1) % len(self.options)
            event.app.invalidate()
            return

        if key == "up":
            self._selected_index = (self._selected_index - 1) % len(self.options)
        elif key == "down":
            self._selected_index = (self._selected_index + 1) % len(self.options)
        elif key in ("escape", "c-c"):
            self._cancel()
            self._clear_buffer(event.current_buffer)
        elif key == "enter":
            self._submit_selection(event.current_buffer)
        elif key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(self.options):
                self._selected_index = idx
                self._submit_selection(event.current_buffer)

        event.app.invalidate()

    def _submit_selection(self, buffer: Any) -> None:
        opt = self.options[self._selected_index]
        label = opt.get("label", "")

        if self.allow_free_text and label == self.free_text_label:
            self._awaiting_free_text = True
            return

        self._result = {"selection": label}
        self._done = True
        self._future.set_result(self._result)

    def _cancel(self) -> None:
        self._cancelled = True
        self._done = True
        self._future.set_result(None)

    @staticmethod
    def _clear_buffer(buffer: Any) -> None:
        if buffer.text:
            buffer.set_document(Document(text="", cursor_position=0), bypass_readonly=True)

    async def wait_for_result(self) -> dict[str, Any] | None:
        return await self._future


class TextModalDelegate:
    """Modal delegate for simple text input."""

    modal_priority = 10

    def __init__(self, question: str) -> None:
        self.question = question
        self._done = False
        self._cancelled = False
        self._future_lazy: asyncio.Future[str | None] | None = None

    @property
    def _future(self) -> asyncio.Future[str | None]:
        """Lazily created on first use so construction works outside a running loop."""
        if self._future_lazy is None:
            self._future_lazy = asyncio.get_running_loop().create_future()
        return self._future_lazy

    def render_modal_body(self, columns: int) -> AnyFormattedText:
        lines = [
            Text.from_markup(f"[yellow]? {self.question}[/yellow]"),
            Text(""),
            Text("  Type your answer, then press Enter to submit.", style="dim italic"),
        ]
        panel = Panel(
            Group(*lines),
            border_style="grey50",
            title="[bold]input[/bold]",
            title_align="left",
            padding=(0, 1),
        )
        ansi = _render_to_ansi(panel, columns=columns)
        return ANSI(ansi.rstrip("\n"))

    def modal_hides_input_buffer(self) -> bool:
        return False

    def modal_allows_text_input(self) -> bool:
        return True

    def should_handle_modal_key(self, key: str) -> bool:
        if self._done or self._cancelled:
            return False
        return key in {"enter", "escape", "c-c"}

    def handle_modal_key(self, key: str, event: KeyPressEvent) -> None:
        if self._done or self._cancelled:
            return

        if key == "enter":
            text = event.current_buffer.text.strip()
            self._done = True
            self._future.set_result(text)
            self._clear_buffer(event.current_buffer)
        elif key in ("escape", "c-c"):
            self._cancel()
            self._clear_buffer(event.current_buffer)

        event.app.invalidate()

    def _cancel(self) -> None:
        self._cancelled = True
        self._done = True
        self._future.set_result(None)

    @staticmethod
    def _clear_buffer(buffer: Any) -> None:
        if buffer.text:
            buffer.set_document(Document(text="", cursor_position=0), bypass_readonly=True)

    async def wait_for_result(self) -> str | None:
        return await self._future


class PasswordModalDelegate:
    """Modal password input — looks like the approval/question panel; buffer digits masked."""

    modal_priority = 12

    def __init__(self, question: str, *, hint: str = "") -> None:
        self.question = question
        self.hint = hint
        self._get_buffer_text: Any | None = None
        self._done = False
        self._cancelled = False
        self._future_lazy: asyncio.Future[str | None] | None = None

    @property
    def _future(self) -> asyncio.Future[str | None]:
        """Lazily created on first use so construction works outside a running loop."""
        if self._future_lazy is None:
            self._future_lazy = asyncio.get_running_loop().create_future()
        return self._future_lazy

    def set_buffer_getter(self, fn: Any) -> None:
        self._get_buffer_text = fn

    def render_modal_body(self, columns: int) -> AnyFormattedText:
        typed = ""
        if self._get_buffer_text is not None:
            typed = str(self._get_buffer_text() or "")
        mask = "•" * len(typed) if typed else ""
        lines: list[Any] = [
            Text.from_markup(f"[bold]{self.question}[/bold]"),
            Text(""),
        ]
        if self.hint:
            lines.append(Text(self.hint, style="dim"))
            lines.append(Text(""))
        lines.append(Text.from_markup(f"[cyan]→ 主密码: {mask}█[/cyan]"))
        lines.append(Text(""))
        lines.append(Text("  输入后 Enter 提交；Esc 取消。密码不会进入对话。", style="dim italic"))
        panel = Panel(
            Group(*lines),
            border_style="grey50",
            title="[bold]vault[/bold]",
            title_align="left",
            padding=(0, 1),
        )
        ansi = _render_to_ansi(panel, columns=columns)
        return ANSI(ansi.rstrip("\n"))

    def modal_hides_input_buffer(self) -> bool:
        # Keep buffer writable like free-text input; panel shows masked bullets.
        return False

    def modal_allows_text_input(self) -> bool:
        return True

    def should_handle_modal_key(self, key: str) -> bool:
        if self._done or self._cancelled:
            return False
        return key in {"enter", "escape", "c-c"}

    def handle_modal_key(self, key: str, event: KeyPressEvent) -> None:
        if self._done or self._cancelled:
            return
        if key == "enter":
            text = event.current_buffer.text
            self._done = True
            self._future.set_result(text)
            self._clear_buffer(event.current_buffer)
        elif key in ("escape", "c-c"):
            self._cancel()
            self._clear_buffer(event.current_buffer)
        event.app.invalidate()

    def _cancel(self) -> None:
        self._cancelled = True
        self._done = True
        self._future.set_result(None)

    @staticmethod
    def _clear_buffer(buffer: Any) -> None:
        if buffer.text:
            buffer.set_document(Document(text="", cursor_position=0), bypass_readonly=True)

    async def wait_for_result(self) -> str | None:
        return await self._future

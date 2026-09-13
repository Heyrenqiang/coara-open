"""Wire CLI scrollback/diff hooks into the kernel frontend adapter."""

from __future__ import annotations

from typing import Any

from src.cli.scrollback import CliScrollback
from src.cli.streaming import notify_block_rendered_after_tool_line
from src.cli.terminal_width import display_width
from src.cli.theme import get_diff_colors
from src.coara.frontend import configure_frontend


def wire_cli_frontend_hooks(*, console: Any | None = None) -> None:
    """Register real CLI implementations for kernel ``get_frontend()`` hooks.

    Attach CLI runs in a separate process from the daemon kernel; tool diff
    frames are queued client-side and rendered via ``render_terminal_blocks``,
    which reads these hooks. Without this call they stay no-op defaults.
    """
    configure_frontend(
        display_width=display_width,
        get_diff_colors=get_diff_colors,
        scrollback_write_renderable=CliScrollback.write_renderable,
        scrollback_write=CliScrollback.write,
        notify_block_rendered_after_tool_line=notify_block_rendered_after_tool_line,
        console=console,
    )

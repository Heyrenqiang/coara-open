"""UI-agnostic slash command service layer.

This package contains the *logic* for every slash command (``/new``, ``/ws``,
``/model`` etc.). Each command is implemented as an async function that takes the
``RootCoara`` instance plus parsed ``args`` and returns a
:class:`src.coara.commands.types.CommandResult`.

Frontends (CLI / Web / Matrix) must NOT import individual command modules. They go
through :func:`src.coara.commands.registry.execute_command` — the single entry point.
This keeps the rendering concerns (``console.print`` / WebSocket / Matrix text) out
of the command logic, so all three frontends share one implementation.
"""

from __future__ import annotations

from src.coara.commands.registry import (
    RUN_WHILE_BUSY,
    execute_command,
    is_run_while_busy_command,
    parse_command,
)
from src.coara.commands.types import CommandAction, CommandResult

__all__ = [
    "CommandAction",
    "CommandResult",
    "RUN_WHILE_BUSY",
    "execute_command",
    "is_run_while_busy_command",
    "parse_command",
]

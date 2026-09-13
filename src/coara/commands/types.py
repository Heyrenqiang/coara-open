"""UI-agnostic command result types.

Commands executed via :func:`src.coara.commands.registry.execute_command` return a
``CommandResult``. The result carries a human-readable plain-text ``output`` (no rich
markup — frontends apply their own styling) plus optional structured ``data`` that
frontends can use for richer rendering. ``action`` lets the frontend react to
side-effects that require follow-up (e.g. clearing CLI scrollback on a new session).

This is the single contract between the command service layer and every frontend
(CLI / Web / Matrix). No frontend imports command modules directly — they all go
through ``execute_command``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CommandAction = Literal[
    "none",  # no follow-up action required
    "new_session",  # a new session was started; frontend may clear display
    "switch_workspace",  # active workspace changed; frontend may reset context
    "restart",  # process restart requested; frontend should exit/shutdown
    "exit",  # user requested to exit the session
]


@dataclass
class CommandResult:
    """Structured outcome of a slash command.

    Attributes
    ----------
    output:
        Human-readable plain text (no rich/markup). Frontends style it themselves.
    action:
        Side-effect signal. See :data:`CommandAction`.
    data:
        Structured payload for rich rendering. Frontends MAY ignore it and just
        print ``output``. Common keys depend on the command.
    exit_session:
        When True the frontend should terminate the chat session (``/exit``).
    """

    output: str = ""
    action: CommandAction = "none"
    data: dict[str, Any] = field(default_factory=dict)
    exit_session: bool = False

    @classmethod
    def text(cls, output: str, **data: Any) -> CommandResult:
        """Convenience: plain text output, no action, optional structured data."""
        return cls(output=output, data=dict(data))

    @classmethod
    def error(cls, message: str, **data: Any) -> CommandResult:
        """Convenience: error output (still plain text; frontend adds red color)."""
        return cls(output=message, data={"error": True, **data})

    @classmethod
    def exit_(cls, message: str = "再见！") -> CommandResult:
        """Convenience: exit the session."""
        return cls(output=message, exit_session=True, action="exit")

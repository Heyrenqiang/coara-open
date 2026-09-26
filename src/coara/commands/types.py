"""UI-agnostic command result types"""

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
    """Structured outcome of a slash command"""

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

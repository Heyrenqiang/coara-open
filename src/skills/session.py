"""Per-coara session skill state (activated skills this session)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SkillSessionState:
    """Session-scoped skill context; cleared on ``/new`` / ``start_new_session``."""

    activated: set[str] = field(default_factory=set)

    def clear(self) -> None:
        self.activated.clear()

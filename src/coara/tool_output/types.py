"""Display block types for CLI-only tool output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DisplayBlockKind = Literal["diff"]


@dataclass(slots=True)
class DiffDisplayBlock:
    """One diff hunk region for a single file (kimi-cli compatible shape)."""

    path: str
    old_text: str
    new_text: str
    old_start: int = 1
    new_start: int = 1
    is_summary: bool = False
    is_new_file: bool = False
    kind: DisplayBlockKind = "diff"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "old_text": self.old_text,
            "new_text": self.new_text,
            "old_start": self.old_start,
            "new_start": self.new_start,
            "is_summary": self.is_summary,
            "is_new_file": self.is_new_file,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffDisplayBlock:
        return cls(
            path=str(data.get("path") or ""),
            old_text=str(data.get("old_text") or ""),
            new_text=str(data.get("new_text") or ""),
            old_start=int(data.get("old_start") or 1),
            new_start=int(data.get("new_start") or 1),
            is_summary=bool(data.get("is_summary")),
            is_new_file=bool(data.get("is_new_file")),
        )


DisplayBlock = DiffDisplayBlock

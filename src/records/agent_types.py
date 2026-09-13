"""Memory entry types and enums."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MemoryType = Literal[
    "event",
    "decision",
    "fact",
    "procedure",
    "reflection",
    "context",
    "profile",
    "preference",
]

Scope = Literal["session", "workspace", "user", "global"]
Sensitivity = Literal["public", "internal", "personal", "secret"]
SourceType = Literal["explicit", "inferred", "synthesized"]
Status = Literal["active", "archived", "superseded", "deleted"]

MEMORY_TYPES: frozenset[str] = frozenset(
    {"event", "decision", "fact", "procedure", "reflection", "context", "profile", "preference"}
)

TYPE_DIR_MAP: dict[str, str] = {
    "event": "events",
    "decision": "decisions",
    "fact": "facts",
    "procedure": "procedures",
    "reflection": "reflections",
    "context": "contexts",
    "profile": "profiles",
    "preference": "preferences",
}


class MemorySource(BaseModel):
    """Provenance for a memory entry.

    ``workspace`` + ``tape_start``/``tape_end``（epoch 秒）指向该条记录所
    概括的录像带原文区间，由 record 工具写入时自动加盖（不经 LLM 手写）；
    需要原文时按坐标回放（session_log.replay）。
    """

    session_id: str = ""
    turn_index: int = 0
    actor: Literal["user", "assistant", "system"] = "assistant"
    workspace: str = ""
    tape_start: float | None = None
    tape_end: float | None = None


class MemoryEntry(BaseModel):
    """One memory document (frontmatter + Markdown body)."""

    id: str
    type: MemoryType
    scope: Scope = "user"
    sensitivity: Sensitivity = "internal"
    created_at: datetime
    source: MemorySource = Field(default_factory=MemorySource)

    title: str = ""
    content: str = ""

    source_type: SourceType = "explicit"
    tags: list[str] = Field(default_factory=list)
    content_hash: str = ""

    ttl_days: int | None = 180
    last_accessed: datetime | None = None
    access_count: int = 0

    status: Status = "active"
    superseded_by: str | None = None
    supersedes: str | None = None

    valid_until: datetime | None = None
    version: int = 1

    # Relative path under agent records root (set by store)
    path: str = ""


class GateResult(BaseModel):
    """Result of the write gate."""

    allowed: bool
    reason: str = ""
    source_type: SourceType = "explicit"
    sensitivity: Sensitivity = "internal"


class DedupeResult(BaseModel):
    """Result of pre-write dedupe check."""

    action: Literal["allow", "skip", "review"] = "allow"
    reason: str = ""
    existing_id: str | None = None

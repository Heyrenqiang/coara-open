"""收藏库条目类型。

见 docs/收藏库设计.md：用户主动收藏的资料库，与记忆系统（agent 主动记录）
物理隔离。收藏零摩擦，消化（摘要+打标）由 agent 在入库时完成。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CollectionSourceType = Literal["link", "file", "snippet", "note"]

COLLECTION_SOURCE_TYPES: frozenset[str] = frozenset({"link", "file", "snippet", "note"})


class CollectionEntry(BaseModel):
    """One collected item (frontmatter + Markdown body)."""

    id: str
    created_at: datetime

    source_type: CollectionSourceType = "snippet"
    source_url: str = ""

    title: str = ""
    summary: str = ""
    note: str = ""
    content: str = ""

    tags: list[str] = Field(default_factory=list)
    content_hash: str = ""

    last_accessed: datetime | None = None
    access_count: int = 0

    # Relative path under the collection root (set by store)
    path: str = ""

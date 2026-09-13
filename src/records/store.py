"""Unified records store under ``<coara_home>/users/default/records/``.

Layout::

    records/
      agent/     # agent records (type dirs, digests, archive, index)
      user/      # user collections (entries/, index)
"""

from __future__ import annotations

from pathlib import Path

from src.records.agent_store import MemoryStore
from src.records.user_store import CollectionStore


class RecordsStore:
    """Single physical root; agent vs user are origin subtrees."""

    def __init__(self, root: Path, *, agent_enabled: bool = True) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        self.agent: MemoryStore | None = None
        if agent_enabled:
            self.agent = MemoryStore(self.root / "agent")

        self.user = CollectionStore(self.root / "user")

    @property
    def agent_root(self) -> Path | None:
        return None if self.agent is None else self.agent.root

"""Process-wide TodoStore cache and per-session asyncio locks."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path

from src.todos.store import TodoStore

# Bound the process-wide cache so long-lived processes don't grow it without
# limit across sessions; eviction is LRU (oldest first). store 与 lock 同住
# 一个缓存条目、同键共生逐出——分开缓存逐出错位会让同会话出现双实例双锁，
# 写互斥失效丢更新。
_MAX_CACHE_ENTRIES = 64


class _SessionEntry:
    """同一会话的 store 与锁，作为一个缓存条目一起逐出。"""

    __slots__ = ("lock", "store")

    def __init__(self, store: TodoStore) -> None:
        self.store = store
        self.lock = asyncio.Lock()


_cache: OrderedDict[tuple[str, str], _SessionEntry] = OrderedDict()


def _session_key(workspace_dir: Path | None, session_id: str) -> tuple[str, str]:
    base = Path(workspace_dir) if workspace_dir else Path.home()
    return (str(base.resolve()), session_id)


def _get_entry(workspace_dir: Path | None, session_id: str) -> _SessionEntry:
    key = _session_key(workspace_dir, session_id)
    entry = _cache.get(key)
    if entry is None:
        entry = _SessionEntry(TodoStore(workspace_dir=workspace_dir, session_id=session_id))
        _cache[key] = entry
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    else:
        _cache.move_to_end(key)
    return entry


def get_todo_store(workspace_dir: Path | None, session_id: str) -> TodoStore:
    """Return a cached TodoStore for this workspace session (one in-memory instance)."""
    return _get_entry(workspace_dir, session_id).store


def get_todo_store_lock(workspace_dir: Path | None, session_id: str) -> asyncio.Lock:
    """Async lock serializing todo read/write for one session."""
    return _get_entry(workspace_dir, session_id).lock


def reset_todo_store_registry() -> None:
    """Clear cached stores and locks (tests)."""
    _cache.clear()

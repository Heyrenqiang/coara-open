"""Resolve the canonical TaskStore root for a workspace."""

from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from src.background.task_store import TaskStore
from src.core.coara_home import resolve_coara_home

# 进程级实例缓存：TaskStore 的锁与内存缓存只在实例内生效，每调用新建实例
# 会让两个写者对同一 task_id 的 load-modify-save 交错丢更新。LRU 上限逐出。
_MAX_CACHE_ENTRIES = 64

_store_cache: OrderedDict[str, TaskStore] = OrderedDict()


def task_store_for_workspace(
    workspace_dir: Path | str | None = None,
    *,
    configured_home: Path | str | None = None,
) -> TaskStore:
    """Return a cached TaskStore under the resolved coara home (global home or ``<workspace>/.coara``)."""
    root = Path(workspace_dir or Path.cwd()).expanduser().resolve()
    home = Path(resolve_coara_home(root, configured_home)).expanduser().resolve()
    key = os.path.normcase(str(home))
    store = _store_cache.get(key)
    if store is None:
        store = TaskStore(home)
        _store_cache[key] = store
        while len(_store_cache) > _MAX_CACHE_ENTRIES:
            _store_cache.popitem(last=False)
    else:
        _store_cache.move_to_end(key)
    return store


def reset_task_store_cache() -> None:
    """Clear cached instances (tests)."""
    _store_cache.clear()


def task_store_for_coara(coara: object) -> TaskStore:
    """TaskStore for a runtime node, honoring ``workspace_manager.coara_home`` when set."""
    workspace_dir = getattr(coara, "workspace_dir", None) or Path.cwd()
    wm = getattr(coara, "workspace_manager", None)
    configured_home = getattr(wm, "coara_home", None) if wm else None
    return task_store_for_workspace(workspace_dir, configured_home=configured_home)


def default_task_store() -> TaskStore:
    """TaskStore for the active CLI process (respects ``COARA_HOME``)."""
    return task_store_for_workspace(Path.cwd())

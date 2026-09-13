"""Tool result caching system."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.core.tool_base import ToolResult


def tool_execution_cache_scope(coara: Any) -> str:
    """Scope executor-level tool cache entries to a workspace + session.

    Executor cache keys are ``{scope}:{tool}:{params}``. Different projects or
    sessions never share entries; switching projects is a natural scope change
    (cache miss on first call, then warm within that session). No need to flush
    the whole cache on ``switch_workspace``.
    """
    workspace = Path(getattr(coara, "workspace_dir", Path.cwd())).expanduser().resolve()
    session_id = str(getattr(coara, "session_id", "") or "")
    return f"{workspace}|{session_id}"


@dataclass
class ToolCacheEntry:
    """A cached tool result."""

    tool_name: str
    params: dict[str, Any]
    result: ToolResult
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds


class ToolCache:
    """Cache for tool execution results."""

    def __init__(self, max_size: int = 1000, default_ttl: float = 300.0):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, ToolCacheEntry] = OrderedDict()
        self._tool_index: dict[str, set[str]] = {}
        self._access_count = 0
        self._tool_ttls: dict[str, float] = {
            "web_search": 5.0,
            "web_fetch": 300.0,
            "read": 60.0,
            "grep": 120.0,
            "glob": 120.0,
        }

    def _generate_key(self, tool_name: str, params: dict[str, Any], *, scope: str = "") -> str:
        normalized = json.dumps(params, sort_keys=True, ensure_ascii=True)
        if scope:
            return f"{scope}:{tool_name}:{normalized}"
        return f"{tool_name}:{normalized}"

    def get(self, tool_name: str, params: dict[str, Any], *, scope: str = "") -> ToolResult | None:
        self._access_count += 1
        if self._access_count % 100 == 0:
            self._cleanup_expired()

        key = self._generate_key(tool_name, params, scope=scope)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.is_expired:
            self._remove_key(key, entry.tool_name)
            logger.debug(f"Cache entry expired: {tool_name}")
            return None

        self._cache.move_to_end(key)
        logger.debug(f"Cache hit: {tool_name}")
        # Return a shallow copy: callers (executor/read) attach per-call
        # metadata (duration_ms, cache_hit...) after the fact — the cached
        # original must stay pristine for the next hit.
        cached = entry.result
        return ToolResult(
            content=cached.content,
            is_error=cached.is_error,
            metadata=dict(cached.metadata),
            cancelled=cached.is_cancelled,
            display=list(cached.display),
        )

    def set(
        self,
        tool_name: str,
        params: dict[str, Any],
        result: ToolResult,
        ttl: float | None = None,
        *,
        scope: str = "",
    ) -> None:
        if result.is_error:
            return
        if len(self._cache) >= self.max_size:
            self._evict_lru()

        key = self._generate_key(tool_name, params, scope=scope)
        entry = ToolCacheEntry(
            tool_name=tool_name,
            params=dict(params),
            result=result,
            ttl_seconds=ttl or self._tool_ttls.get(tool_name, self.default_ttl),
        )
        self._cache[key] = entry
        self._cache.move_to_end(key)
        self._tool_index.setdefault(tool_name, set()).add(key)
        logger.debug(f"Cached result: {tool_name}")

    def invalidate(self, tool_name: str | None = None) -> int:
        if tool_name is None:
            count = len(self._cache)
            self._cache.clear()
            self._tool_index.clear()
            logger.info(f"Cache cleared: {count} entries")
            return count

        keys_to_remove = list(self._tool_index.get(tool_name, set()))
        for key in keys_to_remove:
            self._remove_key(key, tool_name)

        logger.info(f"Cache invalidated for {tool_name}: {len(keys_to_remove)} entries")
        return len(keys_to_remove)

    def invalidate_matching(
        self,
        tool_name: str,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> int:
        keys_to_remove = [
            key for key in list(self._tool_index.get(tool_name, set())) if predicate(self._cache[key].params)
        ]
        for key in keys_to_remove:
            self._remove_key(key, tool_name)

        if keys_to_remove:
            logger.info(f"Cache invalidated for {tool_name} by predicate: {len(keys_to_remove)} entries")
        return len(keys_to_remove)

    def _cleanup_expired(self) -> None:
        expired_items = [(key, entry.tool_name) for key, entry in self._cache.items() if entry.is_expired]
        for key, tool_name in expired_items:
            self._remove_key(key, tool_name)
        if expired_items:
            logger.debug(f"Cleaned up {len(expired_items)} expired cache entries")

    def _evict_lru(self) -> None:
        if not self._cache:
            return
        lru_key = next(iter(self._cache))
        self._remove_key(lru_key, self._cache[lru_key].tool_name)

    def _remove_key(self, key: str, tool_name: str) -> None:
        self._cache.pop(key, None)
        tool_keys = self._tool_index.get(tool_name)
        if tool_keys is None:
            return
        tool_keys.discard(key)
        if not tool_keys:
            del self._tool_index[tool_name]


tool_cache = ToolCache()

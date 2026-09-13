from __future__ import annotations

from src.core.tool_base import ToolResult
from src.tools.cache import ToolCache


def test_tool_cache_invalidate_removes_only_selected_tool_entries() -> None:
    cache = ToolCache()
    cache.set("read", {"path": "a.txt"}, ToolResult.success("a"))
    cache.set("write", {"path": "b.txt"}, ToolResult.success("b"))

    removed = cache.invalidate("read")

    assert removed == 1
    assert cache.get("read", {"path": "a.txt"}) is None
    assert cache.get("write", {"path": "b.txt"}) is not None


def test_tool_cache_invalidate_matching_removes_only_matching_params() -> None:
    cache = ToolCache()
    cache.set("read", {"path": "a.txt", "offset": 0}, ToolResult.success("a0"))
    cache.set("read", {"path": "a.txt", "offset": 10}, ToolResult.success("a10"))
    cache.set("read", {"path": "b.txt", "offset": 0}, ToolResult.success("b0"))

    removed = cache.invalidate_matching("read", lambda params: params.get("path") == "a.txt")

    assert removed == 2
    assert cache.get("read", {"path": "a.txt", "offset": 0}) is None
    assert cache.get("read", {"path": "a.txt", "offset": 10}) is None
    assert cache.get("read", {"path": "b.txt", "offset": 0}) is not None


def test_tool_cache_scope_isolates_same_params_across_workspaces() -> None:
    cache = ToolCache()
    cache.set("grep", {"pattern": "foo"}, ToolResult.success("nx-hit"), scope="D:/nx|s1")
    cache.set("grep", {"pattern": "foo"}, ToolResult.success("pora-hit"), scope="D:/pora|s2")

    assert cache.get("grep", {"pattern": "foo"}, scope="D:/nx|s1").content == "nx-hit"
    assert cache.get("grep", {"pattern": "foo"}, scope="D:/pora|s2").content == "pora-hit"
    assert cache.get("grep", {"pattern": "foo"}, scope="D:/pora|s2").content != "nx-hit"
    # Legacy unscoped entries (e.g. read tool internal cache) keep working.
    cache.set("read", {"path": "a.txt"}, ToolResult.success("a"))
    assert cache.get("read", {"path": "a.txt"}) is not None

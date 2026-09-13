"""Peer WorkspaceSession must not lose tools the old Root-as-default had."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.real_env_helpers import managed_initialized_root

# Tools the startup foreground must keep after peer-session binding.
_REQUIRED_FOREGROUND_TOOLS = frozenset(
    {
        "read",
        "write",
        "edit",
        "web_search",
        "web_fetch",
        "todo",
        "delegate",
        "ws",
        "skill",
        "plan_mode",
    }
)


@pytest.mark.asyncio
async def test_initialized_foreground_is_peer_session_with_full_tool_surface(tmp_path: Path) -> None:
    async with managed_initialized_root(tmp_path) as root:
        fg = root.foreground_coara
        assert fg is not root
        assert root._foreground_session_id is not None
        assert root._foreground_session_id in root._sessions

        names = set(fg._tool_manager.tools.keys())
        missing = sorted(_REQUIRED_FOREGROUND_TOOLS - names)
        assert not missing, f"foreground missing tools: {missing}"

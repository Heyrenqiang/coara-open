"""Runtime-view helper for the dashboard REST handlers (via ``/api/state``).

The old ``/ws/cli`` ingest WebSocket was removed with standalone dashboard mode.
"""

from __future__ import annotations

from typing import Any


def build_runtime_view(runtime: dict[str, Any]) -> dict[str, Any]:
    """Shape the runtime dict served by ``/api/state``."""
    return dict(runtime)

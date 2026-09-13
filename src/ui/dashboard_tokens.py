"""Dashboard token storage shared by server and CLI clients."""

from __future__ import annotations

import os
import secrets
from contextlib import suppress
from pathlib import Path

from src.core.json_store import write_text_atomic

_MAX_TOKEN_BYTES = 1024


def load_or_create_dashboard_token(workspace_dir: Path | str, coara_home: Path | str | None = None) -> str:
    # Token is shared across workspaces under the same coara Home.
    # Storing per-workspace caused Web UI sessions to break on workspace switch:
    # the browser holds a token from workspace A, but after switching to B the
    # server expected B's token, producing "Invalid or missing token".
    # A single home-level token keeps REST + WS auth stable across switches.
    if coara_home is not None:
        data_dir = Path(coara_home) / "system"
    else:
        # No coara Home: fall back to workspace-local traces dir (single-ws mode).
        from src.core.coara_home import resolve_trace_data_dir

        data_dir = resolve_trace_data_dir(workspace_dir, coara_home)
    token_path = data_dir / "dashboard_token"
    # Oversized files are treated as invalid (same as unreadable/empty tokens).
    if token_path.exists() and token_path.stat().st_size <= _MAX_TOKEN_BYTES:
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    write_text_atomic(token_path, token)
    with suppress(OSError):  # best-effort on Windows
        os.chmod(token_path, 0o600)
    return token

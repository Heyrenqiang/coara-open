"""Register remote file-send tool on a Root runtime (Matrix transport adapter)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.matrix_client.file_bridge import MatrixFileBridge
from src.tools.builtin.integration.outbound_file import (
    ensure_outbound_file_router,
    register_send_file_on_root,
)


def register_remote_file_tools(
    root: Any,
    *,
    bridge: MatrixFileBridge,
    workspace_root: Path | str,
) -> None:
    """Attach Matrix delivery to the shared router and register ``send_file``.

    Does **not** clear a previously attached Web bridge — CLI+Web+Matrix can
    all stay wired. Workspace sessions receive the same router instance.
    """
    router = ensure_outbound_file_router(root)
    router.set_matrix(bridge)
    register_send_file_on_root(root, workspace_root=workspace_root, router=router)

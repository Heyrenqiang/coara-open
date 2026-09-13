"""Detect pytest / temp workspaces that should not pollute the registry or CLI."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def is_ephemeral_workspace_path(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    if "pytest-of-" in normalized or "/temp/pytest" in normalized:
        return True
    # Anything under the system temp directory is ephemeral by definition.
    temp_root = os.path.normcase(str(Path(tempfile.gettempdir()).resolve()))
    candidate = os.path.normcase(str(Path(path).resolve()))
    return candidate == temp_root or candidate.startswith(temp_root + os.sep)

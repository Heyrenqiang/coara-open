"""Workspace trash for the delete tool.

Deleted workspace files move into ``<workspace>/.coara/trash/`` preserving
their relative path structure, with a timestamp suffix so same-name files
never overwrite each other. Entries older than ``TRASH_TTL_SECONDS`` are
pruned lazily (on each trashed delete). Vault ``open/`` files and anything
outside the workspace never enter the trash — they are deleted permanently
by the caller
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

TRASH_DIR_NAME = "trash"
TRASH_TTL_SECONDS = 7 * 24 * 3600


def trash_root_for(workspace_root: Path) -> Path:
    """Resolve ``<workspace>/.coara/trash/``."""
    return Path(workspace_root).resolve() / ".coara" / TRASH_DIR_NAME


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def should_use_trash(path: Path, workspace_root: Path) -> bool:
    """True when *path* is a regular workspace file eligible for the trash.

    Excluded (caller deletes permanently instead):
    - vault ``open/`` files — trashing plaintext vault content would weaken
      the seal semantics
    - files already inside the trash — a second delete is final
    - anything outside the workspace root
    """
    from src.vault.guard import is_under_vault_open

    resolved = path.resolve()
    if is_under_vault_open(resolved):
        return False
    root = Path(workspace_root).resolve()
    if not _is_within(root, resolved):
        return False
    return not _is_within(trash_root_for(root), resolved)


def move_to_trash(path: Path, workspace_root: Path) -> Path:
    """Move *path* into the workspace trash, preserving relative structure.

    The destination keeps the file's relative directory and appends a
    timestamp suffix (``name.ext.YYYYmmddHHMMSSffffff``). mtime is refreshed
    to the trash-arrival time so TTL pruning measures time-in-trash, not
    the file's original modification time
    """
    root = Path(workspace_root).resolve()
    rel = path.resolve().relative_to(root)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    dest = trash_root_for(root) / rel.parent / f"{rel.name}.{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    os.utime(dest, None)
    return dest


def prune_expired_trash(
    workspace_root: Path,
    *,
    ttl_seconds: float = TRASH_TTL_SECONDS,
    now: float | None = None,
) -> int:
    """Delete trashed files older than the TTL. Returns the removed count."""
    trash_root = trash_root_for(workspace_root)
    if not trash_root.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for entry in trash_root.rglob("*"):
        if not entry.is_file():
            continue
        try:
            if now - entry.stat().st_mtime > ttl_seconds:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    # 清掉腾空目录（自底向上），回收站根保留
    dirs = sorted(
        (p for p in trash_root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for entry in dirs:
        try:
            entry.rmdir()
        except OSError:
            continue
    return removed

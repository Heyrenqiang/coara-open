"""Cross-source path dedupe helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.event_sources.types import InboundEvent


def path_dedupe_key(workspace: str, path: str | Path) -> str:
    return f"path:{workspace}:{Path(path).expanduser().resolve()}"


def paths_dedupe_key(workspace: str, paths: list[str | Path]) -> str:
    """Stable dedupe key for a batch of paths (hash of the sorted set)."""
    digest = hashlib.sha256(
        "\n".join(sorted(str(Path(p).expanduser().resolve()) for p in paths)).encode("utf-8")
    ).hexdigest()
    return f"paths:{workspace}:{digest}"


def event_paths(event: InboundEvent) -> list[Path]:
    paths: list[Path] = []
    raw_path = event.payload.get("path")
    if raw_path:
        paths.append(Path(str(raw_path)))
    for item in event.payload.get("paths") or []:
        paths.append(Path(str(item)))
    return paths


def event_dedupe_key(event: InboundEvent) -> str:
    paths = event_paths(event)
    if not paths:
        return event.dedupe_key
    if len(paths) == 1:
        return path_dedupe_key(event.workspace, paths[0])
    return paths_dedupe_key(event.workspace, paths)

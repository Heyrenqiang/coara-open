"""Protect sealed vault ciphertext; allow ``open/`` only while unlocked."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

from src.vault.errors import VaultLockedError

# Root sets are only ever replaced with new immutable snapshots so concurrent
# readers never observe a half-mutated set (no lock needed on the read path).
_protected_roots: frozenset[Path] = frozenset()
_open_roots: frozenset[Path] = frozenset()
_activity_touch: Callable[[], None] | None = None

# Serialize open/ tree mutations against seal+wipe (idle lock runs in a worker
# thread while the event loop may still drive file tools). Writers under open/
# must take this lock and re-check admission; seal takes it for the whole
# seal_from_open → wipe_open critical section.
_open_tree_lock = threading.RLock()

_DENY = "路径位于加密保险柜内。请先 `vault(action=open)` 解锁，再在返回的 open 目录里用普通文件工具操作。"
_SEALING_DENY = "保险柜正在封箱或已锁定，无法写入 open 目录；请重新 vault(action=open) 后再试"

T = TypeVar("T")


def register_vault_root(vault_dir: Path | str) -> None:
    global _protected_roots
    _protected_roots = _protected_roots | {Path(vault_dir).expanduser().resolve()}


def unregister_vault_root(vault_dir: Path | str) -> None:
    global _protected_roots
    _protected_roots = _protected_roots - {Path(vault_dir).expanduser().resolve()}


def set_vault_open_root(open_dir: Path | str | None) -> None:
    """While unlocked, register the plaintext working tree (clears previous)."""
    global _open_roots
    _open_roots = frozenset() if open_dir is None else frozenset({Path(open_dir).expanduser().resolve()})


def set_activity_touch(callback: Callable[[], None] | None) -> None:
    """Called on each allowed access under ``open/`` to refresh idle TTL."""
    global _activity_touch
    _activity_touch = callback


def clear_vault_roots() -> None:
    global _protected_roots, _open_roots
    _protected_roots = frozenset()
    _open_roots = frozenset()
    set_activity_touch(None)


def vault_open_tree_lock() -> threading.RLock:
    """Exclusive lock covering open/ writes and seal+wipe."""
    return _open_tree_lock


@contextlib.contextmanager
def vault_open_tree_mutation(path: Path | str) -> Iterator[None]:
    """Hold the open-tree lock for a mutation under a vault root.

    Non-vault paths are a no-op (no lock). Vault paths re-check open-root
    admission under the lock so a seal that already closed admission cannot
    lose an in-flight write: either the write finishes before seal reads, or
    the write fails closed after seal has begun.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        yield
        return
    if not is_under_vault(resolved):
        yield
        return
    with _open_tree_lock:
        if not is_under_vault_open(resolved):
            raise VaultLockedError(_SEALING_DENY)
        yield


def mutate_vault_open_tree(path: Path | str, fn: Callable[[], T]) -> T:
    """Run *fn* under :func:`vault_open_tree_mutation`."""
    with vault_open_tree_mutation(path):
        return fn()


def is_under_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_under_vault(path: Path | str) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    return any(is_under_path(resolved, root) for root in _protected_roots)


def is_under_vault_open(path: Path | str) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    return any(is_under_path(resolved, root) for root in _open_roots)


def _note_open_activity() -> None:
    if _activity_touch is None:
        return
    with contextlib.suppress(Exception):
        _activity_touch()


def vault_path_denied(path: Path | str) -> str | None:
    """Deny sealed vault paths; allow ``open/`` while unlocked (and refresh idle)."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return None
    if is_under_vault_open(resolved):
        _note_open_activity()
        return None
    if is_under_vault(resolved):
        return _DENY
    return None


def _find_path_mention(text: str, needle: str) -> bool:
    """True when *needle* occurs in *text* ending on a path boundary.

    Boundary = end of text or a path separator, so a protected root like
    ``D:\\vault`` no longer matches a mention of ``D:\\vault2`` (path-prefix
    overlap false positive).
    """
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            return False
        end = idx + len(needle)
        if end == len(text) or text[end] in ("\\", "/"):
            return True
        start = idx + 1


def vault_text_mentions_denied(text: str) -> str | None:
    if not text or not _protected_roots:
        return None
    lowered = text.replace("/", "\\").casefold()
    for root in _protected_roots:
        for open_root in _open_roots:
            open_variants = {
                str(open_root).replace("/", "\\").casefold(),
                str(open_root).replace("\\", "/").casefold(),
            }
            if any(v and _find_path_mention(lowered, v) for v in open_variants):
                _note_open_activity()
                return None
        variants = {
            str(root).replace("/", "\\").casefold(),
            str(root).replace("\\", "/").casefold(),
        }
        for v in variants:
            if not v:
                continue
            if _find_path_mention(lowered, v):
                open_marker = f"{v}\\open"
                forward = lowered.replace("\\", "/")
                if (
                    _find_path_mention(lowered, open_marker) or _find_path_mention(forward, f"{v}/open")
                ) and _open_roots:
                    _note_open_activity()
                    return None
                return _DENY
    return None

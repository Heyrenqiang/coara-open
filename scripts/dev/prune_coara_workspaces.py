"""Prune stale coara_home/workspaces trees. Usage: scripts/dev/README.md"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.coara_home import workspace_id_for  # noqa: E402

_STALE_PREFIXES = ("test_", "workspace-", "traces-", "_test")
_LEGACY_EMPTY_HOME_DIRS = ("traces", "memory", "outbox", "skills", "uploads")
_LEGACY_NESTED_COARA = ".coara"
# Registered ids are always kept; also keep test-coara-* when listed in registry.


def _load_registry_ids(coara_home: Path) -> set[str]:
    registry_path = coara_home / "registry" / "workspaces.yaml"
    if not registry_path.is_file():
        return set()
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    workspaces = data.get("workspaces") or {}
    ids: set[str] = set()
    for entry in workspaces.values():
        if isinstance(entry, dict):
            workspace_id = str(entry.get("id") or "").strip()
            if workspace_id:
                ids.add(workspace_id)
            path = entry.get("path")
            if path:
                ids.add(workspace_id_for(path))
    return ids


def _should_remove_workspace_dir(name: str, *, keep_ids: set[str], only_stale: bool) -> bool:
    if name in keep_ids:
        return False
    if only_stale:
        if name.startswith("test-") and name not in keep_ids:
            return True
        return name.startswith(_STALE_PREFIXES)
    return True


def _file_count(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*") if _.is_file())


def _prune_empty_workspace_index(workspace_home: Path, *, dry_run: bool) -> int:
    # `workspace_index/` 是历史遗留数据目录（对应已删除的 src/runtime/workspace_index.py）；
    # 用户磁盘上可能真实存在，这里继续负责清理空目录。
    removed = 0
    index_dir = workspace_home / "workspace_index"
    if not index_dir.is_dir():
        return 0
    if _file_count(index_dir) == 0:
        removed += 1
        if not dry_run:
            shutil.rmtree(index_dir)
    return removed


def _remove_tree(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink()


def prune_coara_home(
    coara_home: Path,
    *,
    keep_ids: set[str] | None = None,
    dry_run: bool = False,
    only_stale: bool = False,
) -> dict[str, int]:
    coara_home = coara_home.resolve()
    keep = set(keep_ids or ())
    keep |= _load_registry_ids(coara_home)

    stats = {
        "workspaces_removed": 0,
        "workspace_index_removed": 0,
        "legacy_home_dirs_removed": 0,
        "nested_coara_removed": 0,
    }

    workspaces_root = coara_home / "workspaces"
    if workspaces_root.is_dir():
        for child in sorted(workspaces_root.iterdir()):
            if not child.is_dir():
                continue
            if not _should_remove_workspace_dir(child.name, keep_ids=keep, only_stale=only_stale):
                stats["workspace_index_removed"] += _prune_empty_workspace_index(child, dry_run=dry_run)
                continue
            stats["workspaces_removed"] += 1
            _remove_tree(child, dry_run=dry_run)

    for name in _LEGACY_EMPTY_HOME_DIRS:
        path = coara_home / name
        if path.is_dir() and _file_count(path) == 0:
            stats["legacy_home_dirs_removed"] += 1
            _remove_tree(path, dry_run=dry_run)

    nested = coara_home / _LEGACY_NESTED_COARA
    if nested.is_dir():
        stats["nested_coara_removed"] = 1
        _remove_tree(nested, dry_run=dry_run)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune stale coara Home workspace directories.")
    parser.add_argument(
        "--coara-home",
        type=Path,
        default=Path(os.environ.get("COARA_HOME", "D:/coara")),
        help="coara Home root (default: COARA_HOME or D:/coara)",
    )
    parser.add_argument("--keep", action="append", default=[], help="Extra workspace_id to preserve (repeatable).")
    parser.add_argument(
        "--only-stale",
        action="store_true",
        help="Remove only pytest/ephemeral prefixes (test-*, workspace-*, etc.), not every unregistered id.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without deleting.")
    args = parser.parse_args()

    stats = prune_coara_home(
        args.coara_home,
        keep_ids=set(args.keep),
        dry_run=args.dry_run,
        only_stale=args.only_stale,
    )
    mode = "dry-run" if args.dry_run else "applied"
    print(f"[{mode}] coara_home={args.coara_home.resolve()}")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

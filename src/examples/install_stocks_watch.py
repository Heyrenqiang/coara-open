"""Install stocks-watch example configs into coara Home."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from src.core.coara_home import resolve_coara_home
from src.workspace.registry import WorkspaceRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "examples" / "stocks-watch"
STOCKS_WATCH_NAME = "stocks-watch"


@dataclass(slots=True)
class InstallResult:
    coara_home: Path
    copied: list[Path]
    registry_updated: bool
    dry_run: bool


def resolve_stocks_watch_coara_home(explicit: Path | str | None = None, *, cwd: Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return resolve_coara_home(cwd or Path.cwd())


def _copy_if_exists(src: Path, dst: Path, *, dry_run: bool, copied: list[Path]) -> bool:
    if not src.exists():
        return False
    if dry_run:
        copied.append(dst)
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    copied.append(dst)
    return True


def _ensure_workspace_registry(
    coara_home: Path,
    name: str,
    workspace_path: Path,
    *,
    dry_run: bool,
) -> bool:
    if dry_run:
        return True

    registry = WorkspaceRegistry(coara_home)
    registry.load()
    registry.ensure_workspace(
        workspace_path.resolve(),
        name=name,
        summary="stocks-watch example (installed by coara examples install stocks-watch)",
    )
    return True


def install_stocks_watch(
    *,
    coara_home: Path | str | None = None,
    workspace: Path | str | None = None,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> InstallResult:
    if not EXAMPLE_DIR.is_dir():
        raise FileNotFoundError(f"example dir not found: {EXAMPLE_DIR}")

    home = resolve_stocks_watch_coara_home(coara_home, cwd=cwd)
    ws = Path(workspace or (EXAMPLE_DIR / "workspace")).expanduser().resolve()
    name = STOCKS_WATCH_NAME
    copied: list[Path] = []

    _copy_if_exists(
        EXAMPLE_DIR / "workspaces-meta.stocks-watch.yaml",
        home / "workspaces-meta" / f"{name}.yaml",
        dry_run=dry_run,
        copied=copied,
    )
    _copy_if_exists(
        EXAMPLE_DIR / "event-inbox-watch.yaml.example",
        home / "users" / "default" / "matters" / "definitions" / f"{name}-inbox-watch.yaml",
        dry_run=dry_run,
        copied=copied,
    )
    registry_updated = _ensure_workspace_registry(home, name, ws, dry_run=dry_run)

    return InstallResult(
        coara_home=home,
        copied=copied,
        registry_updated=registry_updated,
        dry_run=dry_run,
    )


def format_install_next_steps() -> str:
    return (
        "Next steps:\n"
        "  1. coara ws list   # verify stocks-watch\n"
        "  2. coara chat → /events reload   # after enabling event yaml\n"
        "  3. 定时巡检：给 stocks-watch 配 cron 事件源（kind: cron + cron 表达式 + "
        "message_template 写明要干什么），或直接配 file_watch 事件源监听行情目录"
    )

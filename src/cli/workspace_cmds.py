"""CLI helpers for workspace management."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.cli.theme import fg as _fg
from src.workspace.registry import WorkspaceRegistry, WorkspaceRegistryConflictError, open_registry, registry_path

console = Console()

# CLI processes are short-lived; cache a single registry instance and reload
# only when the backing workspaces.yaml changes on disk (mtime check).
_cli_registry_cache: tuple[str, WorkspaceRegistry, float] | None = None


async def _load_config_coara_home() -> Path | None:
    from src.core.config import config_manager

    if getattr(config_manager, "_config", None) is None:
        await config_manager.load()
    return config_manager.config.coara_home


def _registry_mtime(registry: WorkspaceRegistry) -> float:
    try:
        return registry_path(registry.coara_home).stat().st_mtime
    except OSError:
        return -1.0


def _open_cli_registry(workspace: Path) -> WorkspaceRegistry:
    global _cli_registry_cache
    key = str(workspace.resolve())
    if _cli_registry_cache is not None:
        cached_key, cached_registry, cached_mtime = _cli_registry_cache
        if cached_key == key and cached_mtime == _registry_mtime(cached_registry):
            return cached_registry
    home = asyncio.run(_load_config_coara_home())
    registry = open_registry(workspace, home)
    registry.load()
    _cli_registry_cache = (key, registry, _registry_mtime(registry))
    return registry


def print_workspace_list(workspace: Path) -> None:
    from src.workspace.catalog import resolve_workspace_summary

    registry = _open_cli_registry(workspace)
    entries = list(registry.document.workspaces.values())
    entries.sort(key=lambda e: e.name)

    table = Table(title="工作空间")
    table.add_column("名称")
    table.add_column("简介")
    table.add_column("模型")
    table.add_column("路径")
    table.add_column("模式")
    for entry in entries:
        bound = (entry.provider or "").strip()
        llm = f"{entry.provider}·{entry.model}" if bound and (entry.model or "").strip() else (bound or "默认")
        table.add_row(
            entry.name,
            resolve_workspace_summary(entry),
            llm,
            entry.path,
            entry.mode.value,
        )
    console.print(table)


def register_workspace(
    workspace: Path,
    path: Path,
    *,
    name: str | None = None,
    summary: str | None = None,
) -> None:
    registry = _open_cli_registry(workspace)
    try:
        entry = registry.ensure_workspace(path, name=name, summary=summary)
    except WorkspaceRegistryConflictError as exc:
        console.print(f"[{_fg('status.error')}]{exc}[/{_fg('status.error')}]")
        return
    line = f"[{_fg('status.ok')}]已登记工作空间[/{_fg('status.ok')}] {entry.name} -> {entry.path}"
    if entry.summary.strip():
        line += f"\n[dim]简介：[/dim] {entry.summary.strip()}"
    console.print(line)


def rename_workspace(workspace: Path, name: str, new_name: str) -> None:
    from src.workspace.manager import _migrate_workspace_alias_side_effects

    registry = _open_cli_registry(workspace)
    clean = name.strip()
    entry = registry.resolve_name_or_id(clean)
    if entry is None:
        console.print(f"[{_fg('status.error')}]未找到工作空间 {clean}[/{_fg('status.error')}]")
        return
    old_name = entry.name
    try:
        renamed = registry.rename(entry.id, new_name)
    except WorkspaceRegistryConflictError as exc:
        console.print(f"[{_fg('status.error')}]{exc}[/{_fg('status.error')}]")
        return
    except ValueError as exc:
        console.print(f"[{_fg('status.error')}]{exc}[/{_fg('status.error')}]")
        return
    if renamed is None:
        console.print(f"[{_fg('status.error')}]未找到工作空间 {clean}[/{_fg('status.error')}]")
        return
    if renamed.name != old_name:
        _migrate_workspace_alias_side_effects(registry.coara_home, old_name, renamed.name)
    console.print(
        f"[{_fg('status.ok')}]已重命名[/{_fg('status.ok')}] {old_name} → {renamed.name}"
        f"\n[dim]路径未改：[/dim]{renamed.path}"
    )


def remove_workspace(
    workspace: Path,
    name: str,
    *,
    delete_disk: bool = False,
    assume_yes: bool = False,
) -> None:
    registry = _open_cli_registry(workspace)
    clean = name.strip()
    entry = registry.resolve_name_or_id(clean)
    if entry is None:
        console.print(f"[{_fg('status.error')}]未找到工作空间 {clean}[/{_fg('status.error')}]")
        return
    disk_path = entry.resolved_path()
    display_name = entry.name

    # Guard: refuse to unregister the workspace a live coara process is bound to.
    # The running process only reloads the registry on its next message; pulling
    # the active entry out from under it would strand its foreground session.
    from src.coara.workspace_runtime import load_active_runtime

    runtime = load_active_runtime(registry.coara_home)
    if runtime is not None:
        try:
            runtime_path = Path(runtime.workspace_path).expanduser().resolve()
        except OSError:
            runtime_path = None
        if runtime_path is not None and runtime_path == disk_path:
            console.print(
                f"[{_fg('status.error')}]拒绝移除[/{_fg('status.error')}] {display_name}：正被运行中的 coara 进程占用"
                f"（pid {runtime.pid}）。请先在该进程内切换到其他工作空间。"
            )
            return

    if delete_disk and not assume_yes:
        import click

        if not click.confirm(f"确认删除磁盘目录 {disk_path}？此操作不可逆", default=False):
            console.print("[{_fg('status.warn')}]已取消[/{_fg('status.warn')}] 未做任何改动")
            return

    try:
        registry.remove(entry.id)
    except WorkspaceRegistryConflictError as exc:
        console.print(f"[{_fg('status.error')}]{exc}[/{_fg('status.error')}]")
        return
    if not delete_disk:
        console.print(
            f"[{_fg('status.warn')}]已取消登记[/{_fg('status.warn')}]"
            f" 工作空间 {display_name}（磁盘目录未删除）"
        )
        return

    import shutil

    if not disk_path.exists():
        console.print(
            f"[{_fg('status.warn')}]已取消登记[/{_fg('status.warn')}]"
            f" {display_name}；磁盘路径已不存在：{disk_path}"
        )
        return
    try:
        if disk_path.resolve() == registry.coara_home.resolve():
            console.print(
                f"[{_fg('status.warn')}]已取消登记[/{_fg('status.warn')}]"
                f" {display_name}，但拒绝删除 coara_home"
            )
            return
    except OSError:
        pass
    if not disk_path.is_dir():
        console.print(f"[{_fg('status.warn')}]已取消登记[/{_fg('status.warn')}] {display_name}；路径不是目录，未删盘")
        return
    try:
        shutil.rmtree(disk_path)
    except OSError as exc:
        console.print(f"[{_fg('status.error')}]已取消登记，但删盘失败[/{_fg('status.error')}] {disk_path}（{exc}）")
        return
    console.print(f"[{_fg('status.warn')}]已取消登记并删除磁盘[/{_fg('status.warn')}] {display_name} → {disk_path}")

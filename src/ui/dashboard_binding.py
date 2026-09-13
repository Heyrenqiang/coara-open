"""Resolve which workspace directory the dashboard should read traces from."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.coara.workspace_runtime import ActiveWorkspaceRuntime, load_active_runtime
from src.core.coara_home import (
    CoaraHomePaths,
    resolve_bootstrap_coara_home,
    resolve_coara_home,
    resolve_trace_data_dir,
    workspace_id_for,
)


@dataclass(frozen=True, slots=True)
class DashboardBinding:
    workspace: Path
    coara_home: Path
    trace_data_dir: Path
    workspace_id: str
    source: str  # active_runtime | workspace_flag
    active_runtime: ActiveWorkspaceRuntime | None = None


def resolve_configured_coara_home(explicit: Path | str | None = None) -> Path:
    """Prefer explicit/config_manager over a stale ``COARA_HOME`` environment variable."""
    if explicit is not None:
        raw = str(explicit).strip()
        if raw:
            return Path(raw).expanduser().resolve()

    try:
        from src.core.config import config_manager

        if config_manager._config is not None:
            raw_cfg = config_manager.config.coara_home
            if raw_cfg is not None:
                return Path(str(raw_cfg)).expanduser().resolve()
    except Exception:
        pass

    bootstrap = resolve_bootstrap_coara_home()
    if bootstrap is not None:
        return bootstrap

    raw_env = os.environ.get("COARA_HOME", "").strip()
    if raw_env:
        return Path(raw_env).expanduser().resolve()

    return resolve_coara_home(Path.cwd(), None)


def resolve_dashboard_binding(
    workspace: Path | str,
    *,
    coara_home: Path | str | None = None,
    prefer_active_runtime: bool = True,
) -> DashboardBinding:
    """Pick trace source: live ``active.json`` workspace when available, else CLI ``--workspace`` / cwd."""
    home = resolve_configured_coara_home(coara_home)
    fallback = Path(workspace).expanduser().resolve()
    uses_global_home = CoaraHomePaths.for_workspace(fallback, configured_home=home, migrate=False).uses_global_home

    runtime = load_active_runtime(home) if uses_global_home and prefer_active_runtime else None
    if runtime is not None:
        workspace_path = Path(runtime.workspace_path).expanduser().resolve()
        if workspace_path.is_dir():
            trace_dir = resolve_trace_data_dir(workspace_path, configured_home=home)
            return DashboardBinding(
                workspace=workspace_path,
                coara_home=home,
                trace_data_dir=trace_dir,
                workspace_id=runtime.workspace_id or workspace_id_for(workspace_path),
                source="active_runtime",
                active_runtime=runtime,
            )

    trace_dir = resolve_trace_data_dir(fallback, configured_home=home)
    return DashboardBinding(
        workspace=fallback,
        coara_home=home,
        trace_data_dir=trace_dir,
        workspace_id=workspace_id_for(fallback),
        source="workspace_flag",
        active_runtime=None,
    )

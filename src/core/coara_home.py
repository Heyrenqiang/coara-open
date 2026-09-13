"""coara Home path resolution.

Two directory kinds:
  - **Workspace directory** — user workspace (``cwd`` when CLI starts); code and artifacts only.
  - **coara directory** — fixed runtime home (``D:/coara``); traces, logs, vault, registry, and
    machine-wide config under ``system/`` + user assets under ``users/default/``.

When ``coara_home`` is configured, runtime data lives under::

    <coara_home>/system/                  # config.yaml, providers.yaml, .env, skills/, logs/
    <coara_home>/users/default/           # user assets (workflows, matters, assets, works, inbox, …)
    <coara_home>/registry/workspaces.yaml
    <coara_home>/workspaces/<workspace_id>/traces/
    <coara_home>/workspaces/<workspace_id>/logs/coara.log
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SLUG_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")
SYSTEM_SUBDIR = "system"
USERS_SUBDIR = "users"
DEFAULT_USER = "default"


def _logger():
    from src.core.logger import logger

    return logger


def _coerce_existing_path(value: object) -> Path | None:
    """Return a resolved Path for real inputs; ignore mocks and empty strings."""
    from unittest.mock import Mock

    if value is None or isinstance(value, Mock):
        return None
    if isinstance(value, Path):
        return value.expanduser().resolve()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return Path(text).expanduser().resolve()
    return None


def _read_windows_registry_env(name: str) -> list[str]:
    """User then machine ``COARA_HOME`` from Windows registry."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    values: list[str] = []
    hives = (
        winreg.HKEY_CURRENT_USER,
        winreg.HKEY_LOCAL_MACHINE,
    )
    subkeys = (
        r"Environment",
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    )
    for hive in hives:
        for subkey in subkeys:
            if hive == winreg.HKEY_LOCAL_MACHINE and subkey == r"Environment":
                continue
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    reg_value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if isinstance(reg_value, str) and reg_value.strip():
                values.append(reg_value.strip())
    return values


def _iter_coara_home_env_values() -> list[str]:
    """Process ``COARA_HOME`` first, then persistent Windows user/machine values."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw or not raw.strip():
            return
        text = raw.strip()
        if text in seen:
            return
        seen.add(text)
        ordered.append(text)

    add(os.environ.get("COARA_HOME"))
    for value in _read_windows_registry_env("COARA_HOME"):
        add(value)
    return ordered


def _home_has_providers_config(home: Path) -> bool:
    """True when ``system/providers.yaml`` exists under this home."""
    return (system_dir_for_home(home) / "providers.yaml").is_file()


def resolve_bootstrap_coara_home() -> Path | None:
    """Return coara Home from ``COARA_HOME``; prefer a candidate that has ``system/providers.yaml``."""
    candidates: list[Path] = []
    for raw in _iter_coara_home_env_values():
        path = _coerce_existing_path(raw)
        if path is not None and path not in candidates:
            candidates.append(path)
    for home in candidates:
        if _home_has_providers_config(home):
            return home
    return candidates[0] if candidates else None


def system_dir_for_home(home: Path) -> Path:
    """System-level directory: ``<coara_home>/system``.

    Contains machine-wide config (``config.yaml``, ``providers.yaml``, ``.env``),
    built-in skills, and system logs.
    """
    return home / SYSTEM_SUBDIR


def _install_template_dir() -> Path | None:
    """Release install dir ``templates/`` (``COARA_ROOT/templates`` or runtime sibling)."""
    import sys

    root = os.environ.get("COARA_ROOT", "").strip()
    if root:
        candidate = Path(root).expanduser().resolve() / "templates"
        if (candidate / "providers.yaml").is_file():
            return candidate
    exe = Path(sys.executable).resolve()
    if exe.name.startswith("python"):
        # …/InstallDir/runtime/bin/python3 → …/InstallDir/templates
        candidate = exe.parent.parent.parent / "templates"
        if (candidate / "providers.yaml").is_file():
            return candidate
    return None


def ensure_system_config_templates(home: Path) -> list[Path]:
    """Copy ``providers.yaml`` / ``config.yaml`` from install templates if missing.

    Mirrors ``install.sh`` / ``install.ps1`` first-run seeding so a wizard-only
    ``.env`` write cannot leave COARA_HOME without provider definitions.
    """
    created: list[Path] = []
    templates = _install_template_dir()
    if templates is None:
        return created
    system = system_dir_for_home(home)
    system.mkdir(parents=True, exist_ok=True)
    for name in ("providers.yaml", "config.yaml"):
        dst = system / name
        src = templates / name
        if src.is_file() and not dst.is_file():
            shutil.copy2(src, dst)
            created.append(dst)
            _logger().info(f"Created {dst} from install template")
    return created


def user_dir_for_home(home: Path, user: str = DEFAULT_USER) -> Path:
    """User-level directory: ``<coara_home>/users/{user}``.

    Contains all user assets: workflows, matters, assets (vault),
    works, knowledge, inbox, skills, logs, sessions.
    """
    return home / USERS_SUBDIR / user


def resolve_config_home(raw_config: dict[str, Any] | None = None) -> Path:
    """Return coara Home for config I/O: ``coara_home`` in merged config, else ``COARA_HOME``, else ``cwd/.coara``."""
    if raw_config:
        coerced = _coerce_existing_path(raw_config.get("coara_home"))
        if coerced is not None:
            return coerced
    bootstrap = resolve_bootstrap_coara_home()
    if bootstrap is not None:
        return bootstrap
    workspace = _coerce_existing_path(Path.cwd()) or Path.cwd().resolve()
    return (workspace / ".coara").resolve()


def current_coara_home() -> Path | None:
    """``coara_home`` from the loaded config, or ``None`` when config is not loaded.

    Unlike :func:`resolve_config_home` this never falls back to ``COARA_HOME`` or
    ``cwd/.coara``; callers that need a concrete home resolve it themselves.
    """
    from src.core.config import config_manager

    if getattr(config_manager, "_config", None) is None:
        return None
    return getattr(config_manager.config, "coara_home", None)


def iter_home_config_files(home: Path) -> list[Path]:
    """Standard YAML config files.

    Checks ``<coara_home>/system/`` and ``<coara_home>/users/default/config.yaml``
    (user-level override, loaded last, highest priority after llm_preferences).
    """
    names = ("providers.yaml", "config.yaml")
    result: list[Path] = []
    seen_resolved: set[Path] = set()

    def _add_if_file(path: Path) -> None:
        if path.is_file():
            resolved = path.resolve()
            if resolved not in seen_resolved:
                seen_resolved.add(resolved)
                result.append(path)

    sys_dir = system_dir_for_home(home)
    for name in names:
        _add_if_file(sys_dir / name)

    _add_if_file(user_dir_for_home(home) / "config.yaml")

    return result


def home_llm_preferences_path(home: Path) -> Path:
    """Preferred ``llm_preferences.yaml`` location.

    Prefers ``<cwd>/.coara/llm_preferences.yaml``; falls back to
    ``<coara_home>/users/default/llm_preferences.yaml``.
    """
    workspace_path = (Path.cwd() / ".coara" / "llm_preferences.yaml").resolve()
    if workspace_path.is_file():
        return workspace_path
    return user_dir_for_home(home) / "llm_preferences.yaml"


def resolve_coara_home(
    workspace_dir: str | Path,
    configured_home: str | Path | None = None,
) -> Path:
    """Resolve the active coara Home.

    Defaults to ``workspace/.coara``. Set ``COARA_HOME`` or ``coara_home`` in config
    to use a global directory (e.g. ``D:/coara``).
    """
    raw_home = _coerce_existing_path(configured_home) or _coerce_existing_path(os.environ.get("COARA_HOME"))
    if raw_home is not None:
        return raw_home
    workspace = _coerce_existing_path(workspace_dir) or Path.cwd().resolve()
    return (workspace / ".coara").resolve()


def workspace_id_for(workspace_dir: str | Path) -> str:
    """Return a stable, readable workspace id for coara Home storage."""
    resolved = Path(workspace_dir).expanduser().resolve()
    slug = _SLUG_SAFE.sub("-", resolved.name.strip() or "workspace").strip("-").lower()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def _prune_temp_workspace_slots(root: Path) -> None:
    """Drop pytest temp workspace slots left under ``workspaces/``."""
    workspaces_root = root / "workspaces"
    workspaces_root.mkdir(parents=True, exist_ok=True)
    for workspace_dir in list(workspaces_root.iterdir()):
        if not (workspace_dir.is_dir() and workspace_dir.name.startswith("tmp")):
            continue
        try:
            shutil.rmtree(workspace_dir)
        except OSError as exc:
            _logger().warning(f"Could not remove temp workspace slot {workspace_dir}: {exc}")


def ensure_workspace_layout(
    workspace_dir: str | Path,
    configured_home: str | Path | None = None,
) -> CoaraHomePaths:
    """Resolve canonical workspace paths under coara Home."""
    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home=configured_home, migrate=False)
    if paths.uses_global_home:
        _prune_temp_workspace_slots(paths.root)
        # Ensure user asset directory structure exists
        ensure_user_layout(paths.root)
    paths.workspace_home.mkdir(parents=True, exist_ok=True)
    paths.traces_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    if paths.uses_global_home:
        sys_paths = system_paths(paths.root)
        sys_paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.tool_outputs_dir.mkdir(parents=True, exist_ok=True)
    return paths


@dataclass(frozen=True, slots=True)
class CoaraHomePaths:
    """Canonical storage locations for one workspace under coara Home."""

    root: Path
    workspace_dir: Path
    workspace_id: str

    @property
    def uses_global_home(self) -> bool:
        return self.root.resolve() != (self.workspace_dir / ".coara").resolve()

    @classmethod
    def for_workspace(
        cls,
        workspace_dir: str | Path,
        configured_home: str | Path | None = None,
        *,
        migrate: bool = True,
    ) -> CoaraHomePaths:
        workspace = _coerce_existing_path(workspace_dir)
        if workspace is None:
            raise TypeError(f"workspace_dir must be str or Path, got {type(workspace_dir)!r}")
        root = resolve_coara_home(workspace, configured_home)
        paths = cls(root=root, workspace_dir=workspace, workspace_id=workspace_id_for(workspace))
        if migrate and paths.uses_global_home:
            return ensure_workspace_layout(workspace, configured_home=configured_home)
        return paths

    @property
    def workspace_home(self) -> Path:
        if self.uses_global_home:
            return self.root / "workspaces" / self.workspace_id
        return self.root

    @property
    def traces_dir(self) -> Path:
        if self.uses_global_home:
            return self.workspace_home / "traces"
        return self.workspace_dir / ".coara" / "data"

    @property
    def logs_dir(self) -> Path:
        if self.uses_global_home:
            return self.workspace_home / "logs"
        return self.workspace_dir / ".coara" / "logs"

    @property
    def user_paths(self) -> UserPaths:
        """User-level asset paths under ``<coara_home>/users/default/``."""
        return user_paths(self.root)

    @property
    def system_paths(self) -> SystemPaths:
        """System-level paths under ``<coara_home>/system/``."""
        return system_paths(self.root)

    @property
    def subagents_dir(self) -> Path:
        return self.workspace_home / "subagents"

    @property
    def tool_outputs_dir(self) -> Path:
        return self.workspace_home / "tool_outputs"

    @property
    def usage_dir(self) -> Path:
        return self.workspace_home / "usage"

# ============================================================================
# System-level paths (<coara_home>/system/)
# ============================================================================


@dataclass(frozen=True, slots=True)
class SystemPaths:
    """System-level paths under ``<coara_home>/system/``.

    Contains machine-wide configuration, built-in skills, and system logs.
    """

    root: Path

    @property
    def skills_dir(self) -> Path:
        """Built-in / generic skill packs shipped with coara."""
        return self.root / "skills"

    @property
    def logs_dir(self) -> Path:
        """System-level logs (dashboard, tunnels, etc.)."""
        return self.root / "logs"


def system_paths(coara_home: Path) -> SystemPaths:
    """Resolve :class:`SystemPaths` for a given coara home."""
    return SystemPaths(root=system_dir_for_home(coara_home))


# ============================================================================
# User-level paths (<coara_home>/users/{user}/)
# ============================================================================


@dataclass(frozen=True, slots=True)
class UserPaths:
    """User-level paths under ``<coara_home>/users/{user}/``.

    Contains all user assets: workflows, matters, assets (vault),
    works, knowledge, inbox, skills, logs, sessions, and user-level config.
    """

    root: Path
    user: str = DEFAULT_USER

    @property
    def skills_dir(self) -> Path:
        """User skill configuration and custom skills."""
        return self.root / "skills"

    @property
    def tools_dir(self) -> Path:
        """User-level dynamic tool packages（磁盘工具包，挂起池来源之一）。"""
        return self.root / "tools"

    @property
    def skills_custom_dir(self) -> Path:
        return self.skills_dir / "custom"

    @property
    def records_dir(self) -> Path:
        """Unified local records (agent notes + user collects under records/{agent,user}/)."""
        return self.root / "records"

    @property
    def workflows_dir(self) -> Path:
        """User workflow assets (WDL drafts)."""
        return self.root / "workflows"

    @property
    def workflow_drafts_dir(self) -> Path:
        return self.workflows_dir / "drafts"

    @property
    def matters_dir(self) -> Path:
        """User matters (event sources)."""
        return self.root / "matters"

    @property
    def matters_state_dir(self) -> Path:
        """Event-source dedupe state."""
        return self.matters_dir / ".state"

    @property
    def matters_definitions_dir(self) -> Path:
        """Event-source definition YAML files."""
        return self.matters_dir / "definitions"

    @property
    def assets_dir(self) -> Path:
        """User-managed static assets."""
        return self.root / "assets"

    @property
    def vault_dir(self) -> Path:
        """Encrypted vault directory."""
        return self.assets_dir / "vault"

    @property
    def vault_meta_path(self) -> Path:
        return self.vault_dir / "vault.meta.json"

    @property
    def works_dir(self) -> Path:
        """User-created works (PPT, PDF, documents, videos, etc.)."""
        return self.root / "works"

    @property
    def inbox_dir(self) -> Path:
        """Aggregated result inbox for matter run results."""
        return self.root / "inbox"

    @property
    def logs_dir(self) -> Path:
        """User-level logs."""
        return self.root / "logs"

    @property
    def sessions_dir(self) -> Path:
        """Session data."""
        return self.root / "sessions"


def user_paths(coara_home: Path, user: str = DEFAULT_USER) -> UserPaths:
    """Resolve :class:`UserPaths` for a given coara home and user."""
    return UserPaths(root=user_dir_for_home(coara_home, user), user=user)


# 遗留日程卡文件（定时改走 cron 事件源；启动时幂等清除）
_STALE_MATTERS_CARD_FILENAMES = (
    "store.json",
    "store.json.corrupt",
    "runs.jsonl",
)


def purge_stale_matters_card_files(coara_home: Path, user: str = DEFAULT_USER) -> list[Path]:
    """Delete leftover card files under ``matters/`` (idempotent).

    Removes ``store.json`` / ``store.json.corrupt`` / ``runs.jsonl`` only —
    never touches ``definitions/`` or ``.state/``.
    """
    matters = user_paths(coara_home, user).matters_dir
    removed: list[Path] = []
    if not matters.is_dir():
        return removed
    for name in _STALE_MATTERS_CARD_FILENAMES:
        path = matters / name
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            from src.core.logger import logger

            logger.warning(f"Failed to delete stale matters card file {path}: {exc}")
    return removed


def ensure_user_layout(coara_home: Path, user: str = DEFAULT_USER) -> UserPaths:
    """Create the user asset directory structure under coara Home.

    Idempotent — safe to call on every startup.
    """
    paths = user_paths(coara_home, user)
    for directory in (
        paths.root,
        paths.skills_dir,
        paths.skills_custom_dir,
        paths.records_dir,
        paths.workflows_dir,
        paths.workflow_drafts_dir,
        paths.matters_dir,
        paths.matters_definitions_dir,
        paths.matters_state_dir,
        paths.assets_dir,
        paths.vault_dir,
        paths.works_dir,
        paths.inbox_dir,
        paths.logs_dir,
        paths.sessions_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    purge_stale_matters_card_files(coara_home, user)
    return paths


def resolve_trace_data_dir(
    workspace_dir: str | Path,
    configured_home: str | Path | None = None,
) -> Path:
    """Return the directory used for trace JSONL + dashboard runtime state."""
    paths = ensure_workspace_layout(workspace_dir, configured_home=configured_home)
    return paths.traces_dir

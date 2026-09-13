"""Dashboard control-plane helpers (config, skills, meta).

Used by the embedded Web UI server (coara -w) for Settings/Config/Skills APIs.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.config import config_manager, mask_secrets
from src.core.logger import logger
from src.skills.manager import SkillManager, infer_skill_source

if TYPE_CHECKING:
    from src.ui.dashboard_binding import DashboardBinding

# 设置页技能列表是只读展示，不属于任何 CoaraBase 会话：
# 用独立实例做发现，与 per-CoaraBase 的 skill pool 互不干扰
_settings_skill_manager = SkillManager()


def coara_package_version() -> str:
    try:
        return version("coara")
    except PackageNotFoundError:
        return "0.0.0-dev"


_DASHBOARD_BUILD_PATHS: tuple[Path, ...] = (
    Path(__file__).resolve().parent / "dashboard_handlers.py",
    Path(__file__).resolve().parent / "trace_store.py",
    Path(__file__).resolve().parent / "control_plane.py",
    Path(__file__).resolve().parent / "dashboard_ingest.py",
    Path(__file__).resolve().parent / "dashboard_auth.py",
)


def dashboard_build_id() -> str:
    """Fingerprint of dashboard server code; changes when files change."""
    parts: list[str] = []
    for path in _DASHBOARD_BUILD_PATHS:
        if not path.exists():
            continue
        stat = path.stat()
        parts.append(f"{path.name}:{int(stat.st_mtime_ns)}:{stat.st_size}")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def config_revision() -> str:
    """Opaque revision string for config.yaml on disk."""
    from src.core.config import _find_writable_config_yaml, config_manager

    path = _find_writable_config_yaml(getattr(config_manager, "_raw_config", None))
    if path is None or not path.exists():
        return "0"
    stat = path.stat()
    return f"{path.name}:{int(stat.st_mtime_ns)}:{stat.st_size}"


async def discover_skills_payload(workspace_dir: Path, coara_home: Path | None = None) -> list[dict[str, str]]:
    try:
        from src.core.coara_home import resolve_coara_home

        home = coara_home or resolve_coara_home(workspace_dir)
        await _settings_skill_manager.discover(workspace_dir, coara_home=home)
    except Exception as exc:
        logger.warning(f"Skill discovery failed for dashboard: {exc}")
    return [
        {
            "name": s.name,
            "description": s.description,
            "source": infer_skill_source(s.location),
        }
        for s in sorted(_settings_skill_manager.get_all(), key=lambda s: s.name)
    ]


def build_config_envelope(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Masked config payload for Settings API responses."""
    source = raw if raw is not None else config_manager.get_raw_config()
    masked = mask_secrets(source)
    masked.pop("mcp", None)

    skills_raw = masked.get("skills") or {}
    default_include = list(skills_raw.get("default_include") or [])
    return {
        "config": masked,
        "revision": config_revision(),
        "skills": {
            "default_include": default_include,
            "default_exclude": list(skills_raw.get("default_exclude") or []),
        },
    }


def build_meta_payload_from_binding(binding: DashboardBinding) -> dict[str, Any]:
    return {
        "coara_version": coara_package_version(),
        "dashboard_build_id": dashboard_build_id(),
        "workspace": str(binding.workspace),
        "workspace_id": binding.workspace_id,
        "coara_home": str(binding.coara_home),
        "trace_data_dir": str(binding.trace_data_dir),
        "trace_binding_source": binding.source,
        "active_workspace_alias": binding.active_runtime.workspace_name if binding.active_runtime else "",
        "config_revision": config_revision(),
        "capabilities": ["settings", "workflow_editor", "config", "records", "usage"],
    }


def build_providers_envelope() -> dict[str, Any]:
    """Provider definitions for Settings (supports nested or top-level legacy yaml keys).

    Masked via ``mask_secrets`` — users may inline ``api_key`` in providers.yaml
    and it must never echo back to the browser in plaintext.
    """
    raw = config_manager.get_raw_config()
    providers = dict(raw.get("providers") or {})
    if not providers:
        for key, value in raw.items():
            if isinstance(value, dict) and "base_url" in value:
                providers[key] = value
    return {"providers": mask_secrets(providers)}

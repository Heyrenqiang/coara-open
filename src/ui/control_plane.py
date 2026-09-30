"""Dashboard control-plane helpers (config, skills, meta).

Used by the embedded Web UI server for Settings/Config/Skills APIs.
"""

from __future__ import annotations

import asyncio
import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.config import config_manager, mask_secrets
from src.core.errors import SkillError, SkillNotFoundError
from src.core.json_store import write_text_atomic
from src.core.logger import logger
from src.skills.loader import SkillLoader
from src.skills.manager import SkillManager, infer_skill_source

if TYPE_CHECKING:
    from src.core.types import SkillDefinition
    from src.ui.dashboard_binding import DashboardBinding

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


async def discover_skills_payload(workspace_dir: Path, coara_home: Path | None = None) -> list[dict[str, Any]]:
    deferred: set[str] = set()
    try:
        from src.core.coara_home import resolve_coara_home
        from src.core.config import config_manager

        if config_manager._config is None:
            await config_manager.load()
        deferred = set(config_manager.config.skills.deferred)
        home = coara_home or resolve_coara_home(workspace_dir)
        await _settings_skill_manager.discover(workspace_dir, coara_home=home)
    except Exception as exc:
        logger.warning(f"Skill discovery failed for dashboard: {exc}")
    return [
        {
            "name": s.name,
            "description": s.description,
            "source": infer_skill_source(s.location),
            "deferred": s.name in deferred,
        }
        for s in sorted(_settings_skill_manager.get_all(), key=lambda s: s.name)
    ]


async def set_skill_deferred(name: str, deferred: bool, workspace_dir: Path) -> list[str]:
    """把技能加入/移出配置 skills.deferred 并持久化；返回最新名单。

    生效时机：挂起名单在 prompt 构建时读取，改动对新会话生效；
    进行中的会话沿用启动时的清单。
    """
    from src.core.config import config_manager

    if config_manager._config is None:
        await config_manager.load()
    skill = await _resolve_skill(name, workspace_dir)
    if skill is None:
        raise SkillNotFoundError(name)
    current = list(config_manager.get_raw_config().get("skills", {}).get("deferred") or [])
    names = set(current)
    if deferred:
        names.add(skill.name)
    else:
        names.discard(skill.name)
    ordered = sorted(names)
    config_manager.save_config_yaml({"skills": {"deferred": ordered}})
    _settings_skill_manager.apply_deferred(ordered)
    return ordered


class SkillPermissionError(SkillError):
    """技能只读，拒绝在线修改。"""


class SkillValidationError(SkillError):
    """保存前解析校验未通过。"""


async def _resolve_skill(name: str, workspace_dir: Path, coara_home: Path | None = None) -> SkillDefinition | None:
    """按名称解析技能；以发现结果为准（builtin→user→workspace 后者覆盖前者）。"""
    from src.core.coara_home import resolve_coara_home

    home = coara_home or resolve_coara_home(workspace_dir)
    await _settings_skill_manager.discover(workspace_dir, coara_home=home)
    try:
        return _settings_skill_manager.get(name)
    except SkillNotFoundError:
        return None


def _split_skill_frontmatter(raw: str) -> tuple[str, str]:
    """切出 SKILL.md 的 frontmatter 块（含分隔线）与正文。

    无 frontmatter 时返回空块 + 全文；有起始分隔线但无结束线视为非法。
    """
    if not raw.startswith("---"):
        return "", raw
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", raw
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return "".join(lines[: idx + 1]), "".join(lines[idx + 1 :]).lstrip("\r\n")
    raise ValueError("SKILL.md frontmatter 缺少结束分隔线")


async def read_skill_content(name: str, workspace_dir: Path, coara_home: Path | None = None) -> dict[str, Any]:
    """读取技能 SKILL.md 原文，拆分出锁定 frontmatter 与可编辑正文。"""
    skill = await _resolve_skill(name, workspace_dir, coara_home)
    if skill is None:
        raise SkillNotFoundError(name)
    path = Path(skill.location)
    try:
        raw = path.read_text(encoding="utf-8")
        frontmatter_raw, body = _split_skill_frontmatter(raw)
    except (OSError, ValueError) as exc:
        raise SkillError(f"读取技能文件失败：{exc}") from exc
    return {
        "name": skill.name,
        "path": str(path),
        "frontmatter_raw": frontmatter_raw,
        "body": body,
        "readonly": infer_skill_source(skill.location) == "builtin",
    }


async def write_skill_content(
    name: str,
    body: str,
    workspace_dir: Path,
    coara_home: Path | None = None,
    *,
    root: Any | None = None,
) -> dict[str, Any]:
    """写回技能正文：磁盘原 frontmatter 原样保留，仅替换 body；保存前做解析校验。"""
    skill = await _resolve_skill(name, workspace_dir, coara_home)
    if skill is None:
        raise SkillNotFoundError(name)
    if infer_skill_source(skill.location) == "builtin":
        raise SkillPermissionError("内置技能是包内资产，不可在线修改")
    path = Path(skill.location)
    try:
        raw = path.read_text(encoding="utf-8")
        frontmatter_raw, _old_body = _split_skill_frontmatter(raw)
    except (OSError, ValueError) as exc:
        raise SkillError(f"读取技能文件失败：{exc}") from exc

    new_text = frontmatter_raw + "\n" + body.strip() + "\n" if frontmatter_raw else body
    try:
        SkillLoader.parse(new_text, str(path))
    except SkillError as exc:
        raise SkillValidationError(str(exc)) from exc

    await asyncio.to_thread(write_text_atomic, path, new_text)
    from src.core.coara_home import resolve_coara_home

    home = coara_home or resolve_coara_home(workspace_dir)
    await _settings_skill_manager.discover(workspace_dir, coara_home=home, force=True)
    await _refresh_live_skill_managers(root, workspace_dir, home)
    return {"ok": True, "reload": "immediate"}


async def _refresh_live_skill_managers(root: Any | None, workspace_dir: Path, home: Path) -> None:
    """配置页写技能后，刷新内核侧各 SkillManager，避免 activate 仍读旧 body。"""
    if root is None:
        return
    seen: set[int] = set()
    managers: list[Any] = []
    for candidate in (
        getattr(root, "skill_manager", None),
        getattr(getattr(root, "foreground_coara", None), "skill_manager", None),
    ):
        managers.append(candidate)
    sessions = getattr(root, "_sessions", None) or {}
    for session in sessions.values():
        coara = getattr(session, "coara", None)
        managers.append(getattr(coara, "skill_manager", None) if coara is not None else None)
    for sm in managers:
        if sm is None:
            continue
        sid = id(sm)
        if sid in seen:
            continue
        seen.add(sid)
        try:
            await sm.discover(workspace_dir, coara_home=home, force=True)
        except Exception:
            logger.debug("refresh skill_manager after config save failed", exc_info=True)


def build_config_envelope(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Masked config payload for Settings API responses."""
    source = raw if raw is not None else config_manager.get_raw_config()
    masked = mask_secrets(source)
    masked.pop("mcp", None)

    # 实际生效的 coara Home（前端展示用；config.coara_home 为空时也要给出真实落点）
    from src.core.coara_home import resolve_coara_home

    effective_home = resolve_coara_home(Path.cwd(), config_manager.get_raw_config().get("coara_home"))

    skills_raw = masked.get("skills") or {}
    default_include = list(skills_raw.get("default_include") or [])
    return {
        "config": masked,
        "revision": config_revision(),
        "effective_coara_home": str(effective_home),
        "skills": {
            "default_include": default_include,
            "default_exclude": list(skills_raw.get("default_exclude") or []),
            "deferred": list(skills_raw.get("deferred") or []),
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
    """Provider definitions for Settings (supports nested or top-level legacy yaml keys)."""
    import os

    from src.core.api_keys import is_usable_api_key

    raw = config_manager.get_raw_config()
    providers = dict(raw.get("providers") or {})
    if not providers:
        for key, value in raw.items():
            if isinstance(value, dict) and "base_url" in value:
                providers[key] = value
    for _name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        inline = str(cfg.get("api_key") or "").strip()
        env_name = str(cfg.get("api_key_env") or "").strip()
        cfg["has_key"] = is_usable_api_key(inline) or (bool(env_name) and is_usable_api_key(os.getenv(env_name, "")))
    return {"providers": mask_secrets(providers)}

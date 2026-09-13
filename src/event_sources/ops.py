"""Filesystem CRUD for event-source YAML definitions + live reload.

Shared by Web SettingsHandlers and the ``event_source`` LLM tool so both paths
write the same layout and call ``EventSourceManager.reload()``.

布局（2026-08 起）：事件源定义按工作空间存放
``<workspace_dir>/.coara/matters/definitions/{id}.yaml``（经 registry 解析）；
无 registry 时回退旧单一目录 ``<coara_home>/users/default/matters/definitions/``。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from src.core.coara_home import user_paths
from src.core.json_store import write_text_atomic
from src.event_sources.types import EventSourceDefinition

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class EventSourceOpsError(Exception):
    """User-facing validation / IO error for event-source ops."""


def matters_definitions_dir(coara_home: Path | str) -> Path:
    """旧布局单一目录（无 registry 兼容路径）。"""
    return user_paths(Path(coara_home)).matters_definitions_dir


def definition_dirs(coara_home: Path | str, registry: Any | None) -> list[Path]:
    """候选 definitions 目录：registry 布局扫各空间 .coara/matters/definitions/；无 → 旧单一目录。"""
    if registry is not None:
        dirs: list[Path] = []
        try:
            for entry in registry.list_active():
                resolved = getattr(entry, "resolved_path", None)
                path = resolved() if callable(resolved) else None
                if path is not None:
                    dirs.append(Path(path) / ".coara" / "matters" / "definitions")
        except Exception:
            return dirs
        return dirs
    return [matters_definitions_dir(coara_home)]


def _workspace_definitions_dir(coara_home: Path | str, registry: Any | None, workspace: str) -> Path:
    """按 workspace 解析目标目录：registry 布局 → <ws>/.coara/matters/definitions/；无 → 旧单一目录。"""
    if registry is not None:
        resolver = getattr(registry, "resolve_name_or_id", None)
        entry = resolver(workspace) if callable(resolver) else None
        if entry is not None:
            resolved = getattr(entry, "resolved_path", None)
            path = resolved() if callable(resolved) else None
            if path is not None:
                return Path(path) / ".coara" / "matters" / "definitions"
    return matters_definitions_dir(coara_home)


def _find_definition_path(coara_home: Path | str, registry: Any | None, source_id: str) -> Path | None:
    """在所有候选目录里找 id 对应的 yaml 文件（事件源 id 全局唯一）。"""
    eid = validate_source_id(source_id)
    for directory in definition_dirs(coara_home, registry):
        path = directory / f"{eid}.yaml"
        if path.is_file():
            return path
    return None


def validate_source_id(source_id: str) -> str:
    eid = (source_id or "").strip()
    if not _SAFE_ID_RE.match(eid):
        raise EventSourceOpsError(f"非法事件源 id：{source_id!r}（仅字母数字与 ._- ，最长 64）")
    return eid


def yaml_path(coara_home: Path | str, source_id: str, registry: Any | None = None) -> Path:
    """定位事件源 yaml：优先找到现有文件；找不到则用旧单一目录（新建场景）。"""
    existing = _find_definition_path(coara_home, registry, source_id)
    if existing is not None:
        return existing
    eid = validate_source_id(source_id)
    return matters_definitions_dir(coara_home) / f"{eid}.yaml"


def list_definitions(coara_home: Path | str, registry: Any | None = None) -> list[EventSourceDefinition]:
    """列出全部事件源定义（registry 布局遍历所有工作空间，id 全局去重）。"""
    rows: list[EventSourceDefinition] = []
    seen_ids: set[str] = set()
    for root in definition_dirs(coara_home, registry):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                defn = EventSourceDefinition.model_validate(raw)
            except Exception:
                continue
            if defn.id in seen_ids:
                continue
            seen_ids.add(defn.id)
            rows.append(defn)
    return rows


def read_definition(
    coara_home: Path | str,
    source_id: str,
    registry: Any | None = None,
) -> EventSourceDefinition:
    path = _find_definition_path(coara_home, registry, source_id)
    if path is None:
        raise EventSourceOpsError(f"事件源不存在：{source_id}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return EventSourceDefinition.model_validate(raw)
    except EventSourceOpsError:
        raise
    except Exception as exc:
        raise EventSourceOpsError(f"事件源定义无效：{exc}") from exc


def write_definition(
    coara_home: Path | str,
    data: dict[str, Any] | EventSourceDefinition,
    *,
    overwrite: bool,
    registry: Any | None = None,
) -> EventSourceDefinition:
    if isinstance(data, EventSourceDefinition):
        defn = data
    else:
        try:
            defn = EventSourceDefinition.model_validate(data)
        except Exception as exc:
            raise EventSourceOpsError(f"事件源定义无效：{exc}") from exc
    validate_source_id(defn.id)
    directory = _workspace_definitions_dir(coara_home, registry, defn.workspace)
    path = directory / f"{defn.id}.yaml"
    if not overwrite and path.exists():
        raise EventSourceOpsError(f"事件源已存在：{defn.id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = defn.model_dump(mode="json", exclude_none=False)
    write_text_atomic(
        path,
        yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
    )
    return defn


def delete_definition(coara_home: Path | str, source_id: str, registry: Any | None = None) -> None:
    path = _find_definition_path(coara_home, registry, source_id)
    if path is None:
        raise EventSourceOpsError(f"事件源不存在：{source_id}")
    path.unlink()


def set_enabled(
    coara_home: Path | str,
    source_id: str,
    *,
    enabled: bool,
    registry: Any | None = None,
) -> EventSourceDefinition:
    defn = read_definition(coara_home, source_id, registry=registry)
    defn.enabled = enabled
    return write_definition(coara_home, defn, overwrite=True, registry=registry)


async def reload_manager(manager: Any) -> None:
    if manager is None:
        raise EventSourceOpsError("事件源管理器未启动")
    await manager.reload()


def ensure_workspace_registered(workspace_manager: Any, workspace: str) -> None:
    name = (workspace or "").strip()
    if not name:
        raise EventSourceOpsError("workspace 不能为空")
    if workspace_manager is None:
        raise EventSourceOpsError("工作空间功能未启用")
    entry = workspace_manager.registry.resolve_name_or_id(name)
    if entry is None:
        raise EventSourceOpsError(f"工作空间未登记：{name}（先用 /ws 或 ws 工具添加）")

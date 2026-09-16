"""Persistent workspace registry under coara Home."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from src.core.coara_home import resolve_coara_home, workspace_id_for
from src.core.json_store import write_text_atomic
from src.core.logger import logger
from src.workspace.ephemeral import is_ephemeral_workspace_path
from src.workspace.types import (
    MountMode,
    ViewCapability,
    WorkspaceEntry,
    WorkspaceKind,
    WorkspaceRegistryDocument,
    WorkspaceStatus,
)


class WorkspaceRegistryConflictError(RuntimeError):
    """On-disk ``workspaces.yaml`` changed since this instance last loaded/saved.

    Another process (``coara ws`` CLI, another kernel, etc.) won the race.
    Callers should ``load()`` and retry, or surface a clear failure — never
    overwrite silently.
    """


def registry_dir(coara_home: Path) -> Path:
    return coara_home / "registry"


def registry_path(coara_home: Path) -> Path:
    """Canonical workspace registry file."""
    return registry_dir(coara_home) / "workspaces.yaml"


def _registry_disk_token(path: Path) -> str:
    """Content fingerprint of the registry file; empty string if missing.

    Uses ``read_text`` (universal newlines) so the token matches what ``load``
    and in-memory YAML dumps see on Windows, where on-disk bytes may be CRLF.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


class WorkspaceRegistry:
    """Load/save the workspace catalog.

    Dict keys are always ``entry.id`` (never short names or arbitrary YAML keys).

    Saves are compare-and-swap on the file content fingerprint recorded at the
    last successful ``load``/``save``. Concurrent writers (CLI ``coara ws`` vs
    kernel) cannot silently clobber each other: the loser gets
    :class:`WorkspaceRegistryConflictError`.
    """

    def __init__(self, coara_home: Path):
        self.coara_home = coara_home.resolve()
        self._path = registry_path(self.coara_home)
        self.document = WorkspaceRegistryDocument()
        self._disk_token = ""

    def _normalize_keys(self) -> bool:
        """Rewrite dict keys to match entry.id. Returns True if keys changed."""
        changed = False
        normalized: dict[str, WorkspaceEntry] = {}
        for key, entry in self.document.workspaces.items():
            if key != entry.id:
                changed = True
            normalized[entry.id] = entry
        if changed:
            self.document.workspaces = normalized
            if self.document.default_workspace and self.document.default_workspace not in normalized:
                self.document.default_workspace = next(iter(normalized), None)
        return changed

    def load(self) -> WorkspaceRegistryDocument:
        canonical = registry_path(self.coara_home)
        self._path = canonical
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if not canonical.exists():
            self.document = WorkspaceRegistryDocument()
            self._disk_token = ""
            return self.document

        raw_text = canonical.read_text(encoding="utf-8")
        self._disk_token = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        raw = yaml.safe_load(raw_text) or {}
        self.document = WorkspaceRegistryDocument.model_validate(raw)
        self.prune_ephemeral_workspaces()
        if self._normalize_keys():
            self.save()
        return self.document

    def prune_ephemeral_workspaces(self) -> int:
        """Remove pytest temp workspaces from the persisted registry."""
        removed_ids: list[str] = []
        for workspace_id, entry in list(self.document.workspaces.items()):
            if is_ephemeral_workspace_path(entry.resolved_path()):
                removed_ids.append(workspace_id)
                del self.document.workspaces[workspace_id]
        if not removed_ids:
            return 0
        if self.document.default_workspace in removed_ids:
            remaining = next(iter(self.document.workspaces), None)
            self.document.default_workspace = remaining
        self.save()
        logger.info(f"Pruned ephemeral workspaces from registry: {removed_ids}")
        return len(removed_ids)

    def save(self) -> None:
        self._normalize_keys()
        current = _registry_disk_token(self._path)
        if current != self._disk_token:
            raise WorkspaceRegistryConflictError(
                "工作空间登记表已被其他进程改过，本次保存已放弃以免互相覆盖；请重试"
            )
        payload = self.document.model_dump(mode="json", by_alias=True)
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        write_text_atomic(self._path, text)
        # Re-fingerprint from disk (newline translation may differ from *text*)
        self._disk_token = _registry_disk_token(self._path)

    def _name_from_path(self, path: Path) -> str:
        name = path.name.strip() or "workspace"
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.lower())
        return slug.strip("-").strip() or "workspace"

    def _unique_name(self, base: str, existing: set[str]) -> str:
        if base not in existing:
            return base
        index = 2
        while f"{base}-{index}" in existing:
            index += 1
        return f"{base}-{index}"

    def ensure_workspace(
        self,
        path: Path,
        *,
        name: str | None = None,
        summary: str | None = None,
    ) -> WorkspaceEntry:
        """Register *path* if missing; return the entry."""
        resolved = path.expanduser().resolve()
        for entry in self.document.workspaces.values():
            if entry.resolved_path() == resolved:
                if summary is not None and summary.strip() and not entry.summary.strip():
                    entry.summary = summary.strip()
                    if not is_ephemeral_workspace_path(resolved):
                        self.save()
                return entry

        existing_names = {entry.name for entry in self.document.workspaces.values()}
        chosen_name = name or self._unique_name(self._name_from_path(resolved), existing_names)
        if chosen_name in existing_names:
            chosen_name = self._unique_name(chosen_name, existing_names)

        workspace_id = workspace_id_for(resolved)
        entry = WorkspaceEntry(
            name=chosen_name,
            id=workspace_id,
            path=str(resolved),
            kind=WorkspaceKind.MANAGED,
            status=WorkspaceStatus.ACTIVE,
            mode=MountMode.READ_WRITE,
            summary=(summary or "").strip(),
        )
        self.document.workspaces[workspace_id] = entry
        persist = not is_ephemeral_workspace_path(resolved)
        if self.document.default_workspace is None:
            self.document.default_workspace = workspace_id
        if persist:
            self.save()
        logger.info(f"Registered workspace {chosen_name} ({workspace_id}) -> {resolved}")
        return entry

    def get_by_id(self, workspace_id: str) -> WorkspaceEntry | None:
        return self.document.workspaces.get(workspace_id)

    def get_by_name(self, name: str) -> WorkspaceEntry | None:
        for entry in self.document.workspaces.values():
            if entry.name == name:
                return entry
        return None

    def list_active(self, *, include_internal: bool = False) -> list[WorkspaceEntry]:
        """活跃空间列表。internal（系统空间）默认不进用户常规列表。"""
        return [
            entry
            for entry in self.document.workspaces.values()
            if entry.status == WorkspaceStatus.ACTIVE
            and (include_internal or entry.kind != WorkspaceKind.INTERNAL)
        ]

    def ensure_internal_workspace(
        self,
        path: Path,
        *,
        name: str,
        view: ViewCapability | None = None,
        provider: str | None = None,
        model: str | None = None,
        summary: str | None = None,
        content_type: str | None = None,
        storefront: str | None = None,
        home_view: str | None = None,
        persona: str | None = None,
    ) -> WorkspaceEntry:
        """登记 kind=internal 的系统空间（daily 等）；幂等，重复调用原地修正元数据。

        与 ``ensure_workspace`` 同路径即返回语义：条目已存在时仅把 kind/view 等
        系统字段对齐（用户改过 provider/model 绑定则尊重，不覆盖）。internal 条目
        默认不进 ``list_active``（include_internal=True 才取），
        ``default_workspace`` 永远不会被设置为 internal 条目。
        """
        from src.workspace.types import ViewCapability

        resolved = path.expanduser().resolve()
        desired_view = view if view is not None else ViewCapability.ALL
        existing = None
        for entry in self.document.workspaces.values():
            if entry.resolved_path() == resolved:
                existing = entry
                break

        if existing is not None:
            changed = False
            # 条目名是展示名（daily → 记录），系统有权随空间语义演进校正；改 name
            # 不动 id/path——registry 主键是 path，录像带/会话档案均不断链
            desired_name = name.strip()
            if desired_name and existing.name != desired_name and desired_name not in {
                e.name for e in self.document.workspaces.values() if e.id != existing.id
            }:
                existing.name = desired_name
                changed = True
            if existing.kind != WorkspaceKind.INTERNAL:
                existing.kind = WorkspaceKind.INTERNAL
                changed = True
            if existing.view != desired_view:
                existing.view = desired_view
                changed = True
            for field, value in (
                ("content_type", content_type),
                ("storefront", storefront),
                ("home_view", home_view),
                ("persona", persona),
            ):
                if value is not None and getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed = True
            if summary is not None and summary.strip() and not existing.summary.strip():
                existing.summary = summary.strip()
                changed = True
            if changed and not is_ephemeral_workspace_path(resolved):
                self.save()
            return existing

        existing_names = {entry.name for entry in self.document.workspaces.values()}
        chosen_name = name.strip() or "internal"
        if chosen_name in existing_names:
            chosen_name = self._unique_name(chosen_name, existing_names)

        workspace_id = workspace_id_for(resolved)
        entry = WorkspaceEntry(
            name=chosen_name,
            id=workspace_id,
            path=str(resolved),
            kind=WorkspaceKind.INTERNAL,
            status=WorkspaceStatus.ACTIVE,
            mode=MountMode.READ_WRITE,
            view=desired_view,
            summary=(summary or "").strip(),
            provider=provider,
            model=model,
            content_type=content_type,
            storefront=storefront,
            home_view=home_view,
            persona=persona,
        )
        self.document.workspaces[workspace_id] = entry
        if not is_ephemeral_workspace_path(resolved):
            self.save()
        logger.info(f"Registered internal workspace {chosen_name} ({workspace_id}) -> {resolved}")
        return entry

    def remove(self, workspace_id: str) -> bool:
        entry = self.resolve_name_or_id(workspace_id)
        if entry is None:
            return False
        if entry.id not in self.document.workspaces:
            return False
        del self.document.workspaces[entry.id]
        if self.document.default_workspace == entry.id:
            remaining = next(iter(self.document.workspaces), None)
            self.document.default_workspace = remaining
        self.save()
        return True

    def rename(self, workspace_id: str, new_name: str) -> WorkspaceEntry | None:
        """Rename a registered workspace (id/path unchanged). Returns the entry or None."""
        entry = self.resolve_name_or_id(workspace_id)
        if entry is None:
            return None
        chosen = new_name.strip()
        if not chosen:
            raise ValueError("新名称不能为空")
        if chosen == entry.name:
            return entry
        existing = {e.name for e in self.document.workspaces.values() if e.id != entry.id}
        if chosen in existing:
            raise ValueError(f"工作空间名已存在：{chosen}")
        entry.name = chosen
        self.save()
        logger.info(f"Renamed workspace {entry.id} -> {chosen}")
        return entry

    def rebind_path(self, workspace_id: str, new_path: str) -> WorkspaceEntry | None:
        """改绑目录（项目挪了位置）：id 不变，coara_home 侧历史档案不断链。

        目标路径必须已存在且是目录；不与他条目的路径冲突。改名/改绑分离：
        目录被删后用户确认「项目搬走了」时走这里，不重建空目录。
        """
        from pathlib import Path

        entry = self.resolve_name_or_id(workspace_id)
        if entry is None:
            return None
        resolved = Path(new_path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"目录不存在：{resolved}")
        for other in self.document.workspaces.values():
            if other.id != entry.id and other.resolved_path() == resolved:
                raise ValueError(f"该目录已登记为空间：{other.name}")
        if entry.resolved_path() == resolved:
            return entry
        entry.path = str(resolved)
        self.save()
        logger.info(f"Rebound workspace {entry.id} -> {resolved}")
        return entry

    def set_default(self, workspace_id: str) -> bool:
        entry = self.resolve_name_or_id(workspace_id)
        if entry is None:
            return False
        self.document.default_workspace = entry.id
        self.save()
        return True

    def resolve_name_or_id(self, identifier: str) -> WorkspaceEntry | None:
        """Look up by dict key (which is entry.id), then by name."""
        entry = self.document.workspaces.get(identifier)
        if entry is not None:
            return entry
        return self.get_by_name(identifier)


def open_registry(initial_path: Path, configured_home: Path | None = None) -> WorkspaceRegistry:
    home = resolve_coara_home(initial_path, configured_home)
    registry = WorkspaceRegistry(home)
    registry.load()
    return registry

"""工作空间级 LLM 绑定：注册表条目携带 provider/model，缺省跟随全局默认."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.logger import logger

if TYPE_CHECKING:
    from src.workspace.manager import WorkspaceManager
    from src.workspace.types import WorkspaceEntry


def workspace_llm_chrome_fields(target: Any) -> dict[str, str]:
    """同空间模型同步：事件携带空间定位，供各端只刷新 pin 到该空间的 chrome。"""
    from pathlib import Path

    out: dict[str, str] = {
        "session_id": str(getattr(target, "session_id", "") or ""),
        "workspace_dir": str(getattr(target, "workspace_dir", "") or ""),
    }
    manager = getattr(target, "workspace_manager", None)
    if manager is None:
        return out
    try:
        wid = manager.match_path_to_workspace_id(Path(out["workspace_dir"]))
    except Exception:
        return out
    if not wid:
        return out
    out["workspace_id"] = str(wid)
    try:
        entry = manager.registry.get_by_id(wid)
        if entry is not None:
            out["workspace_name"] = str(entry.name or "")
    except Exception:
        logger.debug(f"resolve workspace name for {wid} failed; binding shows id only", exc_info=True)
    return out


def entry_llm_override(entry: WorkspaceEntry | None) -> tuple[str | None, str | None]:
    """Return the entry's validated (provider, model) override; (None, None) = 跟随全局."""
    if entry is None:
        return None, None
    provider = (entry.provider or "").strip() or None
    model = (entry.model or "").strip() or None
    if provider is None:
        return None, None
    from src.llm.registry import provider_registry

    if not provider_registry.has(provider):
        logger.warning(
            f"Workspace '{entry.name}' bound provider '{provider}' is unavailable; falling back to the global default"
        )
        return None, None
    return provider, model


def bind_entry_llm(
    manager: WorkspaceManager,
    workspace_id: str,
    provider: str,
    model: str,
) -> bool:
    """Persist a provider/model binding onto the workspace entry."""
    entry = manager.registry.get_by_id(workspace_id)
    if entry is None:
        return False
    entry.provider = provider
    entry.model = model
    manager.registry.save()
    return True


def touch_entry_llm(
    manager: WorkspaceManager,
    workspace_id: str,
    provider: str,
    model: str,
) -> bool:
    """Write the workspace's last-run LLM (no-op when unchanged)."""
    entry = manager.registry.get_by_id(workspace_id)
    if entry is None:
        return False
    provider = (provider or "").strip()
    model = (model or "").strip()
    if (entry.provider or "").strip() == provider and (entry.model or "").strip() == model:
        return False
    entry.provider = provider or None
    entry.model = model or None
    manager.registry.save()
    return True


def clear_entry_llm(manager: WorkspaceManager, workspace_id: str) -> bool:
    """Remove the binding so the workspace follows the global default again."""
    entry = manager.registry.get_by_id(workspace_id)
    if entry is None:
        return False
    entry.provider = None
    entry.model = None
    manager.registry.save()
    return True

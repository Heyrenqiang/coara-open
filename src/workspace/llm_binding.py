"""工作空间级 LLM 绑定：注册表条目携带 provider/model，缺省跟随全局默认.

解析顺序：条目绑定（provider 须仍存在于 providers.yaml，否则视为未绑定并告警）
→ 全局默认（llm_preferences.yaml）。provider+model 始终成对校验：
绑定里的 provider 失效时整条绑定作废，不会留下 provider/model 错配组合。
"""

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
        pass
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
    """Write the workspace's last-run LLM (no-op when unchanged).

    last-run = 最后一次正常对话（端输入注入）实际使用的模型，是空间内核
    默认模型：重启加载与 janitor 触发都用它。与 bind_entry_llm 的区别是
    内容没变时不重写盘（每次输入都走此路径，避免无谓的 yaml 写盘）。
    """
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

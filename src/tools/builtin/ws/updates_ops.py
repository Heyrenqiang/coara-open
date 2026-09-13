"""Workspace updates actions for the ws tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.core.logger import logger
from src.core.tool_base import ToolResult
from src.workspace.updates.presentation import (
    format_update_message,
    format_updates_board,
    format_updates_list,
    format_updates_stats,
)
from src.workspace.updates.rules import NOTE_REQUIRED_FOR
from src.workspace.updates.store import UpdateFilter, WorkspaceUpdatesStore

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


def resolve_updates_store(coara: CoaraBase | None) -> WorkspaceUpdatesStore | None:
    if coara is None:
        return None
    manager = getattr(coara, "event_source_manager", None)
    if manager is not None and hasattr(manager, "updates_store"):
        return manager.updates_store()
    wm = getattr(coara, "workspace_manager", None)
    if wm is None:
        return None
    return WorkspaceUpdatesStore(wm.coara_home, registry=getattr(wm, "registry", None))


def _reviewed_by(coara: CoaraBase | None) -> str:
    """处置人：janitor 系统代理记的轨迹归 janitor，其余（用户/主会话代操作）归 user。"""
    persona = getattr(getattr(coara, "identity", None), "persona", None)
    if getattr(persona, "name", "") == "janitor":
        return "janitor"
    return "user"


def _note_missing_for(note: str, action: str) -> bool:
    """处置必须写 note（dismiss/elevate/resolve），缺失即拒绝。"""
    return action in NOTE_REQUIRED_FOR and not (note or "").strip()


async def execute_updates_action(
    coara: CoaraBase | None,
    action: str,
    *,
    alias: str | None = None,
    status: str = "unread",
    message_id: str | None = None,
    limit: int = 30,
    note: str = "",
    salience: str = "",
) -> ToolResult:
    store = resolve_updates_store(coara)
    if store is None:
        return ToolResult.error("工作空间动态存储不可用")

    if action == "updates_stats":
        registered: set[str] | None = None
        wm = getattr(coara, "workspace_manager", None)
        if wm is not None:
            registered = {entry.name for entry in wm.registry.document.workspaces.values()}
        return ToolResult.success(format_updates_stats(store.stats(), registered_aliases=registered))

    if action == "updates_list":
        update_status: UpdateFilter = status if status in ("unread", "read", "archived", "all") else "unread"
        messages = store.list_messages(workspace=alias, status=update_status, limit=max(1, limit))
        return ToolResult.success(format_updates_list(messages, status=update_status, alias=alias))

    if action == "updates_pending":
        messages = store.pending(limit=max(1, limit))
        return ToolResult.success(
            format_updates_list(messages, status="unread", alias=None, header="待处理（高显著 · 跨空间）")
        )

    if action == "updates_board":
        pending, reviewed = store.board(workspace=alias, limit=max(1, limit))
        return ToolResult.success(format_updates_board(pending, reviewed, alias=alias or "全部"))

    if action == "updates_read":
        if not message_id:
            return ToolResult.error("updates_read 需要 message_id")
        msg = store.mark_read(message_id)
        if msg is None:
            return ToolResult.error(f"未找到动态：{message_id}")
        return ToolResult.success(format_update_message(msg, header="已读"))

    if action == "updates_archive":
        if not message_id:
            return ToolResult.error("updates_archive 需要 message_id")
        msg = store.archive(message_id)
        if msg is None:
            return ToolResult.error(f"未找到动态：{message_id}")
        return ToolResult.success(f"已归档 {msg.workspace} {msg.message_id}")

    if action == "updates_dismiss":
        if not message_id:
            return ToolResult.error("updates_dismiss 需要 message_id")
        if _note_missing_for(note, "dismiss"):
            return ToolResult.error("updates_dismiss 必须写 note（处置理由）")
        msg = store.set_disposition(message_id, "dismissed", reviewed_by=_reviewed_by(coara), note=note)
        if msg is None:
            return ToolResult.error(f"未找到动态：{message_id}")
        return ToolResult.success(f"已勾掉 {msg.workspace} {msg.message_id}（留处置轨迹）")

    if action == "updates_elevate":
        if not message_id:
            return ToolResult.error("updates_elevate 需要 message_id")
        if _note_missing_for(note, "elevate"):
            return ToolResult.error("updates_elevate 必须写 note（呈阅理由）")
        msg = store.set_disposition(message_id, "elevated", reviewed_by=_reviewed_by(coara), note=note)
        if msg is None:
            return ToolResult.error(f"未找到动态：{message_id}")
        return ToolResult.success(f"已呈阅 {msg.workspace} {msg.message_id}（上浮待处理视图）")

    if action == "updates_resolve":
        if not message_id:
            return ToolResult.error("updates_resolve 需要 message_id")
        if _note_missing_for(note, "resolve"):
            return ToolResult.error("updates_resolve 必须写 note（处置结论）")
        msg = store.set_disposition(message_id, "resolved", reviewed_by=_reviewed_by(coara), note=note)
        if msg is None:
            return ToolResult.error(f"未找到动态：{message_id}")
        return ToolResult.success(f"已处置 {msg.workspace} {msg.message_id}（留处置轨迹）")

    if action == "updates_salience":
        if not message_id:
            return ToolResult.error("updates_salience 需要 message_id")
        level = (salience or "").strip().lower()
        if level not in ("low", "normal", "high"):
            return ToolResult.error("updates_salience 需要 salience=low|normal|high")
        msg = store.set_salience(message_id, level, by=_reviewed_by(coara))
        if msg is None:
            return ToolResult.error(f"未找到动态或显著性非法：{message_id}")
        return ToolResult.success(f"已将 {msg.workspace} {msg.message_id} 显著性改为 {msg.salience}")

    if action == "updates_mark_read":
        target = (alias or "").strip()
        if not target:
            return ToolResult.error("updates_mark_read 需要 name（工作空间名）")
        store.mark_all_read(target)
        await _push_updates_state_after_mark_read(coara)
        return ToolResult.success(f"已把 {target} 的动态全部标为已读")

    return ToolResult.error(f"未知 updates 动作：{action}")


async def _push_updates_state_after_mark_read(coara: CoaraBase | None) -> None:
    """已读水位推进后推送 Matrix 红点状态（仅 Root 上下文有此方法；失败只记日志）。"""
    push = getattr(coara, "_push_workspace_updates_state_to_matrix", None)
    if push is None:
        return
    try:
        await push()
    except Exception as exc:
        logger.debug(f"Updates state push after mark_read skipped: {exc}")


_CLI_ACTION_MAP = {
    "stats": "updates_stats",
    "list": "updates_list",
    "pending": "updates_pending",
    "board": "updates_board",
    "read": "updates_read",
    "archive": "updates_archive",
    "dismiss": "updates_dismiss",
    "elevate": "updates_elevate",
    "resolve": "updates_resolve",
    "salience": "updates_salience",
    "mark_read": "updates_mark_read",
}


async def run_updates_cli(
    coara: CoaraBase | None,
    action: str,
    *,
    alias: str | None = None,
    status: str = "unread",
    message_id: str | None = None,
    limit: int = 30,
    note: str = "",
    salience: str = "",
) -> ToolResult:
    """Run updates action from CLI (/ws updates) with short action names."""
    mapped = _CLI_ACTION_MAP.get(action.strip(), action.strip())
    return await execute_updates_action(
        coara,
        mapped,
        alias=alias,
        status=status,
        message_id=message_id,
        limit=limit,
        note=note,
        salience=salience,
    )

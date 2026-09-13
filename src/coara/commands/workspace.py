"""Workspace commands: /ws (list, switch, rename, default, updates).

Manages VFS workspaces and the active workspace. ``/ws switch`` delegates to
``root.switch_workspace(name)``：为每个工作空间创建或复用对等的
``WorkspaceSession``（独立 ``CoaraBase``、独立 message_history / todos /
文件读状态）。各工作空间会话互不覆盖，切回即恢复。
``/ws rename`` 只改登记名（id/path 不变）；``/ws default`` 只写启动默认标记。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult

if TYPE_CHECKING:
    from src.coara.root import RootCoara


_WS_USAGE = (
    "用法：/ws ｜ /ws list ｜ /ws <序号> ｜ /ws switch <工作空间名或序号> "
    "｜ /ws switch <工作空间名> --default ｜ /ws default <工作空间名> "
    "｜ /ws rename <旧名> <新名> "
    "｜ /ws updates [list|read|archive|dismiss|elevate|resolve|salience|mark_read|stats] "
    "[动态编号] [工作空间名|优先级]"
)


@register("ws")
async def handle_ws(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Handle /ws and its subcommands."""
    manager = getattr(root, "workspace_manager", None)
    if manager is None:
        return CommandResult.error("工作空间功能未启用")

    # /ws or /ws list
    if args.sub is None or args.sub == "list":
        return _build_workspace_list_result(root)

    # /ws <序号> — same as /ws switch <序号>
    if args.sub.isdigit() and not args.value:
        return await _handle_ws_switch(root, args, name_or_index=args.sub)

    if args.sub == "switch":
        return await _handle_ws_switch(root, args)

    if args.sub == "default":
        return _handle_ws_default(root, args)

    if args.sub == "rename":
        return _handle_ws_rename(root, args)

    if args.sub == "updates":
        return await _handle_ws_updates(root, args)

    return CommandResult(_WS_USAGE, data={"usage": True})


async def _handle_ws_updates(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Handle ``/ws updates [动作] [工作空间名]``（默认 list 未读动态）。"""
    from src.tools.builtin.ws.updates_ops import run_updates_cli

    tokens = args.parts[2:]
    action = "list"
    alias: str | None = None
    message_id: str | None = None
    note = ""
    salience = ""
    if tokens:
        head = tokens[0].lower()
        if head in (
            "stats",
            "list",
            "read",
            "archive",
            "dismiss",
            "elevate",
            "resolve",
            "salience",
            "mark_read",
        ):
            action = head
            tokens = tokens[1:]
        if tokens:
            if action in ("read", "archive", "dismiss", "elevate", "resolve"):
                message_id = tokens[0]
                if len(tokens) > 1:
                    note = " ".join(tokens[1:])
            elif action == "salience":
                message_id = tokens[0]
                if len(tokens) > 1:
                    salience = tokens[1].strip().lower()
            else:
                alias = tokens[0].lstrip("@")
    if action in ("read", "archive", "dismiss", "elevate", "resolve") and not message_id:
        return CommandResult.error(f"用法：/ws updates {action} <动态编号> [理由]")
    if action == "salience" and (not message_id or salience not in ("low", "normal", "high")):
        return CommandResult.error("用法：/ws updates salience <动态编号> <low|normal|high>")

    result = await run_updates_cli(
        root,
        action,
        alias=alias,
        message_id=message_id,
        note=note,
        salience=salience,
    )
    if result.is_error:
        return CommandResult.error(result.content)
    return CommandResult(output=result.content)


def _build_workspace_list_result(root: RootCoara) -> CommandResult:
    """Build a plain-text workspace listing plus structured data.

    「当前」按 **cli view** 标注（CLI /help 列表）；手机面板另行用 matrix view
    改写 active（见 mobile_sync.build_workspaces_payload）。
    """
    from src.workspace.catalog import resolve_workspace_summary

    manager = root.workspace_manager
    if manager is None:
        return CommandResult.error("工作空间功能未启用")

    view_id = root.view_workspace_id("cli")
    active_name = ""
    for entry in manager.list_workspaces():
        if entry.id == view_id:
            active_name = entry.name
            break
    active_id = view_id

    workspaces = []
    lines = ["工作空间"]
    for idx, entry in enumerate(manager.list_workspaces(), start=1):
        is_active = entry.id == active_id
        marker = "●" if is_active else "○"
        summary = resolve_workspace_summary(entry)
        line = f"{marker} {idx}. {entry.name}"
        if summary:
            line += f" — {summary}"
        line += f"  {entry.path}"
        lines.append(line)
        workspaces.append(
            {
                "idx": idx,
                "name": entry.name,
                "id": entry.id,
                "summary": summary,
                "path": str(entry.path),
                "mode": entry.mode.value,
                "active": is_active,
                # 空间身份随列表下发：手机端抽屉按 kind/home_view 把空间分到
                # 「用户空间 / 系统空间」两组（与 web navRegistry 同一判据）。
                "kind": entry.kind.value,
                "home_view": entry.home_view or "",
            }
        )
    active_name = active_name or ""
    if active_name:
        lines.append(f"当前：{active_name}")
    lines.append("切换：输入 /ws 后 ↑↓ 选择回车（Esc 取消回 /ws），或 /ws <序号> ｜ /ws switch <名>")
    return CommandResult(
        output="\n".join(lines),
        data={"workspaces": workspaces, "active_name": active_name, "active_id": active_id},
    )


def _resolve_workspace_ref(root: RootCoara, ref: str) -> str | None:
    """Resolve a workspace name, id, or 1-based list index to a switch target.

    Returns the name/id string to pass to ``switch_workspace``, or ``None`` when
    a numeric index is out of range.
    """
    manager = root.workspace_manager
    if manager is None:
        return None
    raw = (ref or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        entries = list(manager.list_workspaces())
        idx = int(raw)
        if 1 <= idx <= len(entries):
            return entries[idx - 1].name
        return None
    return raw


async def _handle_ws_switch(
    root: RootCoara,
    args: CommandArgs,
    *,
    name_or_index: str | None = None,
) -> CommandResult:
    """Switch this end's view workspace by name, id, or list index."""
    name = name_or_index if name_or_index is not None else args.value
    if not name:
        return CommandResult.error("用法：/ws switch <工作空间名或序号> [--default]")

    resolved = _resolve_workspace_ref(root, name)
    if resolved is None:
        return CommandResult.error(f"找不到工作空间序号 {name}", name=name)
    name = resolved

    set_default = args.has_flag("default", "d")
    # 每端独立 view：origin → end 键；一律 set_view_workspace，不走特权前台。
    try:
        end_key = root.normalize_view_end(args.origin_source or "cli")
    except ValueError:
        end_key = "cli"

    if not await root.set_view_workspace(end_key, name):
        return CommandResult.error(f"找不到工作空间 {name} ", name=name)

    view_coara = root.resolve_view_coara(end_key)
    entry = root.workspace_manager.registry.resolve_name_or_id(name) if root.workspace_manager else None
    display_name = entry.name if entry else name
    from src.coara.workspace_state import format_workspace_switch_message

    session_renewed = bool(getattr(root, "last_switch_session_renewed", False)) if end_key == "cli" else False
    last_active = getattr(root, "last_switch_last_active", None) if end_key == "cli" else None
    lines = [
        format_workspace_switch_message(
            display_name,
            session_renewed=session_renewed,
            last_active=last_active,
        ),
        str(view_coara.workspace_dir),
    ]
    data: dict = {
        "name": display_name,
        "workspace_dir": str(view_coara.workspace_dir),
        "session_renewed": session_renewed,
        "view": end_key,
        "workspace_id": entry.id if entry else root.view_workspace_id(end_key),
        "session_id": view_coara.session_id,
        "coara_id": str(
            getattr(view_coara, "coara_id", "")
            or getattr(getattr(view_coara, "identity", None), "coara_id", "")
            or ""
        ),
        "provider_name": str(getattr(view_coara, "provider_name", "") or ""),
        "model_name": str(getattr(view_coara, "model_name", "") or ""),
    }
    with contextlib.suppress(Exception):
        data["is_plan_mode"] = bool(view_coara.is_plan_mode())
    if (
        set_default
        and root.workspace_manager is not None
        and entry is not None
        and root.workspace_manager.set_persistent_default(entry.id)
    ):
        lines.append("已设为默认工作空间")
        data["default_set"] = True
    return CommandResult(
        output="\n".join(lines),
        action="switch_workspace",
        data=data,
    )


def _handle_ws_default(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Set the persistent default workspace by name or id."""
    name = args.value
    if not name:
        return CommandResult.error("用法：/ws default <工作空间名>")

    if root.workspace_manager is None:
        return CommandResult.error("工作空间功能未启用")

    entry = root.workspace_manager.registry.resolve_name_or_id(name)
    if entry is None:
        return CommandResult.error(f"找不到工作空间 {name} ", name=name)

    if root.workspace_manager.set_persistent_default(entry.id):
        return CommandResult(
            output=f"已设 {entry.name} 为默认工作空间",
            data={"default_id": entry.id, "default_name": entry.name},
        )
    return CommandResult.error(f"无法设置默认工作空间 {name} ", name=name)


def _handle_ws_rename(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Rename a registered workspace (path unchanged)."""
    if root.workspace_manager is None:
        return CommandResult.error("工作空间功能未启用")

    tokens = [t for t in args.parts[2:] if t.strip()]
    if len(tokens) < 2:
        return CommandResult.error("用法：/ws rename <旧名> <新名>")

    old_name, new_name = tokens[0], tokens[1]
    entry = root.workspace_manager.registry.resolve_name_or_id(old_name)
    if entry is None:
        return CommandResult.error(f"找不到工作空间 {old_name} ", name=old_name)

    prev = entry.name
    try:
        renamed = root.workspace_manager.rename_workspace(entry.id, new_name)
    except ValueError as exc:
        return CommandResult.error(str(exc))
    if renamed is None:
        return CommandResult.error(f"找不到工作空间 {old_name} ", name=old_name)

    return CommandResult(
        output=f"已重命名：{prev} → {renamed.name}\n路径未改：{renamed.path}",
        data={"old_name": prev, "name": renamed.name, "id": renamed.id, "path": renamed.path},
    )

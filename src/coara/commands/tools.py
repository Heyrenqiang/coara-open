"""Tool/utility commands: /tools /events /usage.

These commands inspect or manage runtime tooling infrastructure (tool visibility,
event sources, token usage).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@register("tools")
async def handle_tools(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Show tool visibility, or enable/disable a tool at runtime (/tools on|off <name>)."""
    # /tools off <name> | /tools on <name> — 运行时开关，三端（CLI/Web/Matrix）共享
    if args.sub in ("on", "off"):
        if not args.value:
            return CommandResult.error("用法：/tools on <名称> ｜ /tools off <名称>")
        name = args.value.strip()
        from src.core.config import config_manager

        try:
            tools_cfg = getattr(config_manager.config, "tools", None)
        except Exception:
            tools_cfg = None
        current = set((tools_cfg.disabled or []) if tools_cfg is not None else [])
        if args.sub == "off":
            current.add(name)
        else:
            current.discard(name)
        ordered = sorted(current)
        try:
            config_manager.set_tools_disabled(ordered)
        except Exception as exc:
            return CommandResult.error(f"写入 config.yaml 失败：{exc}")
        root.apply_tools_disabled(ordered)
        verb = "已停用" if args.sub == "off" else "已启用"
        return CommandResult(
            output=f"{verb} {name}（运行时立即生效，已写入 config.yaml）",
            data={"tool": name, "disabled": args.sub == "off", "disabled_list": ordered},
        )

    vis = root.get_status().get("tool_visibility", {})
    visible = sorted(vis.get("visible", []))
    hidden = vis.get("hidden") or {}

    lines = ["工具一览"]
    if visible:
        lines.append("")
        lines.append("可用")
        for name in visible:
            lines.append(f"  · {name}")
    if hidden:
        lines.append("")
        lines.append("已隐藏")
        zh_reasons = {
            "disabled": "已禁用",
            "not_in_whitelist": "白名单外",
            "owner_only": "仅拥有者",
            "plan_mode_restricted": "计划模式限制",
        }
        for name, reasons in sorted(hidden.items()):
            reason_text = "、".join(zh_reasons.get(str(r), str(r)) for r in reasons) if reasons else "受限"
            lines.append(f"  · {name}（{reason_text}）")
    if not visible and not hidden:
        lines.append("（当前没有可见工具）")

    return CommandResult(
        output="\n".join(lines),
        data={"visible": visible, "hidden": dict(hidden)},
    )


@register("events")
async def handle_events(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Show event source status or reload config."""
    manager = getattr(root, "event_source_manager", None)
    if manager is None:
        return CommandResult.error("事件源功能未启用")

    if args.sub == "reload":
        await manager.reload()
        return CommandResult.text("事件源已重新加载", reloaded=True)

    rows = manager.list_status()
    if not rows:
        return CommandResult(
            output=f"还没有配置事件源\n可在此目录添加 YAML：{manager.primary_config_dir()}",
            data={"sources": []},
        )

    handle_zh = {
        "park": "挂住",
        "janitor": "janitor 过目",
    }
    lines = ["事件源", ""]
    for row in rows:
        state = "运行中" if row["running"] else "待命"
        mode = handle_zh.get(str(row.get("handle") or "park"), str(row.get("handle")))
        salience = str(row.get("salience") or "normal")
        line = f"· {row['id']}（{row['kind']}）· 工作空间 {row['workspace']} · {state} · {mode} · 显著性 {salience}"
        if row.get("webhook_url"):
            line += f"\n  接收地址：{row['webhook_url']}"
        lines.append(line)
    return CommandResult(output="\n".join(lines), data={"sources": rows})


@register("usage")
async def handle_usage(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Show token / tool usage for current session or a time window."""
    # Flush pending usage events so the query sees latest data
    store = getattr(root, "_usage_store", None)
    if store is not None:
        store.flush(timeout=0.5)

    from src.runtime.usage_query import (
        format_session_usage_lines,
        format_summary_text,
        parse_usage_chat_args,
        resolve_usage_path_for_root,
        summarize_session_stats,
        summarize_usage,
    )

    events_path = resolve_usage_path_for_root(root)
    mode, days = parse_usage_chat_args(args.raw.lower())

    if mode == "window":
        summary = summarize_usage(events_path, days=days)
        lines = [f"用量概览 · 最近 {days} 天"]
        lines.append(format_summary_text(summary))
        if store is not None and store.dropped_events:
            lines.append(f"（有 {store.dropped_events} 条用量记录未能写入，可忽略）")
        lines.append("离线查询：coara usage summary")
        return CommandResult(
            output="\n".join(lines),
            data={"mode": "window", "days": days, "summary": summary},
        )

    session_stats = summarize_session_stats(events_path, str(getattr(root, "session_id", "") or ""))
    lines = ["用量概览 · 当前对话"]
    lines.extend(format_session_usage_lines(session_stats))
    lines.append("看最近几天：/usage 7")
    return CommandResult(
        output="\n".join(lines),
        data={"mode": "session", "stats": session_stats},
    )

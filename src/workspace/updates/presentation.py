"""Markdown formatters for ws tool and CLI updates output."""

from __future__ import annotations

from typing import Any

from src.workspace.updates.rules import format_review_rules

_DISPOSITION_ZH = {
    "pending": "待过目",
    "elevated": "呈阅",
    "resolved": "已处置",
    "dismissed": "已勾掉",
}


def format_updates_stats(
    stats: dict[str, dict[str, int]],
    *,
    registered_aliases: set[str] | None = None,
) -> str:
    if registered_aliases is not None:
        stats = {alias: row for alias, row in stats.items() if alias in registered_aliases}
    if not stats:
        return "# 工作空间动态\n\n（暂无动态）"
    lines = ["# 工作空间动态统计", ""]
    for alias in sorted(stats):
        row = stats[alias]
        lines.append(
            f"- **{alias}**：未读 {row.get('unread', 0)} · "
            f"已读 {row.get('read', 0)} · 归档 {row.get('archived', 0)} · 合计 {row.get('total', 0)}"
        )
    lines.append("")
    lines.append("查看列表：`review(action=list, name=<工作空间名>)`")
    return "\n".join(lines)


def format_updates_list(messages: list[Any], *, status: str, alias: str | None, header: str | None = None) -> str:
    ws = f" {alias}" if alias else ""
    status_zh = {"unread": "未读", "read": "已读", "archived": "已归档"}.get(status, status)
    title = header or f"工作空间动态{ws}"
    if not messages:
        return f"# {title}\n\n（暂无{status_zh}动态）"
    lines = [f"# {title}（{status_zh}）", ""]
    for msg in messages:
        flag = {"unread": "未读", "read": "已读", "archived": "已归档"}.get(msg.status, msg.status)
        salience = getattr(msg, "salience", "normal")
        salience_flag = " · 高显著" if salience == "high" else ""
        disposition = getattr(msg, "disposition", "pending")
        disposition_flag = {"elevated": " · 呈阅", "resolved": " · 已处置", "dismissed": " · 已勾掉"}.get(
            disposition, ""
        )
        title_line = (msg.title or "").strip() or "（无标题）"
        lines.append(f"· [{flag}]{salience_flag}{disposition_flag} {title_line}")
        lines.append(f"  {msg.workspace} · {msg.created_at[:19]} · 编号 {msg.message_id}")
        lines.append("")
    lines.append("读全文：`review(action=read, message_id=…)`")
    return "\n".join(lines).rstrip()


def format_update_message(msg: Any, *, header: str) -> str:
    title = (getattr(msg, "title", None) or "").strip()
    head = title or header
    lines = [
        f"# {head} · {msg.workspace}",
        "",
        f"- **来源**：{msg.source_id} · {msg.event_type}",
        f"- **时间**：{msg.created_at}",
        f"- **状态**：{msg.status}",
        f"- **编号**：{msg.message_id}",
        "",
        "## 正文",
        "",
        msg.text.strip(),
    ]
    disposition = getattr(msg, "disposition", "pending")
    history = list(getattr(msg, "review_history", None) or [])
    has_review = disposition != "pending" or bool(history) or bool(getattr(msg, "reviewed_by", ""))
    if has_review:
        lines += [
            "",
            "## 处置轨迹",
            "",
        ]
        if history:
            for entry in history:
                action_zh = _DISPOSITION_ZH.get(getattr(entry, "action", ""), getattr(entry, "action", ""))
                note = (getattr(entry, "note", "") or "").strip()
                lines.append(
                    f"- **{action_zh}** by {getattr(entry, 'by', '') or '—'} · {getattr(entry, 'at', None) or '—'}"
                    + (f"\n  note：{note}" if note else "")
                )
        else:
            # 旧条目无 review_history：回退展示最近一次快捷字段
            disposition_zh = _DISPOSITION_ZH.get(disposition, disposition)
            by = getattr(msg, "reviewed_by", "") or "—"
            at = getattr(msg, "reviewed_at", None) or "—"
            lines.append(f"- **{disposition_zh}** by {by} · {at}")
            note = (getattr(msg, "review_note", "") or "").strip()
            if note:
                lines.append(f"  note：{note}")
    return "\n".join(lines)


def format_updates_board(
    pending: list[Any],
    reviewed: list[Any],
    *,
    alias: str,
) -> str:
    """渲染工作空间过目单：规则头 + 待处理 + 已处置（janitor 一次拿全）。"""
    lines = [
        f"# 工作空间过目单 · {alias}（schema v1）",
        "",
        format_review_rules(),
        "",
    ]
    lines.append(f"## 待处理（{len(pending)}）")
    if not pending:
        lines.append("（无）")
    for i, msg in enumerate(pending, start=1):
        title = (getattr(msg, "title", "") or "").strip()[:40]
        body = (getattr(msg, "display_text", None) or getattr(msg, "text", "") or "").strip()
        head = f"[{i:02d}] {msg.message_id}  {msg.source_id}  {msg.created_at[:16]}  「{title}」"
        lines.append(f"{head}  {msg.salience}  {msg.disposition}")
        if body:
            lines.append(f"      {body[:200]}")
    lines += ["", f"## 已处置（{len(reviewed)}）"]
    if not reviewed:
        lines.append("（无）")
    for i, msg in enumerate(reviewed, start=1):
        history = list(getattr(msg, "review_history", None) or [])
        if history:
            entry = history[-1]
            by = getattr(entry, "by", "") or ""
            at = (getattr(entry, "at", None) or "")[:16]
            action = getattr(entry, "action", "") or ""
            note = (getattr(entry, "note", "") or "").strip()
        else:
            by = getattr(msg, "reviewed_by", "") or ""
            at = (getattr(msg, "reviewed_at", None) or "")[:16]
            action = getattr(msg, "disposition", "") or ""
            note = (getattr(msg, "review_note", "") or "").strip()
        action_zh = _DISPOSITION_ZH.get(action, action)
        title = (getattr(msg, "title", "") or "").strip()[:30]
        lines.append(
            f"[{i:02d}] {msg.message_id}  {action_zh}  by {by} @ {at}  「{title}」"
            + (f"\n      note：{note[:80]}" if note else "")
        )
    lines.append("")
    lines.append("处置：`review(action=dismiss|elevate|resolve, message_id=…, note=…)`")
    lines.append("调优先级：`review(action=salience, message_id=…, salience=low|normal|high)`")
    return "\n".join(lines)

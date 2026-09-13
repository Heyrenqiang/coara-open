"""Formatting helpers for collection tool output."""

from __future__ import annotations

from src.records.user_types import CollectionEntry


def format_collection_results(entries: list[CollectionEntry], *, query: str = "") -> str:
    if not entries:
        return f"收藏库里没有找到与「{query}」相关的资料。" if query else "收藏库还是空的。"
    lines = [f"找到 {len(entries)} 条收藏：", ""]
    for e in entries:
        created = e.created_at.strftime("%Y-%m-%d") if e.created_at else ""
        lines.append(f"### {e.title}（`{e.id}` · {e.source_type} · {created}）")
        if e.source_url:
            lines.append(f"来源: {e.source_url}")
        if e.tags:
            lines.append(f"标签: {', '.join(e.tags)}")
        if e.summary:
            lines.append(e.summary.strip())
        if e.note:
            lines.append(f"注记: {e.note.strip()}")
        lines.append("")
    return "\n".join(lines).strip()


def format_collection_show(entry: CollectionEntry) -> str:
    lines = [f"# {entry.title}", ""]
    created = entry.created_at.strftime("%Y-%m-%d %H:%M")
    lines.append(f"ID: `{entry.id}` · 类型: {entry.source_type} · 收藏于: {created}")
    if entry.source_url:
        lines.append(f"来源: {entry.source_url}")
    if entry.tags:
        lines.append(f"标签: {', '.join(entry.tags)}")
    lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append(entry.summary.strip() or "（无）")
    if entry.note.strip():
        lines.extend(["", "## 用户注记", "", entry.note.strip()])
    if entry.content.strip():
        lines.extend(["", "## 原文", "", entry.content.strip()])
    return "\n".join(lines)

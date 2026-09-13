"""Format recalled memories for model injection (reference, not constraint)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.records.agent_types import MemoryEntry


def _provenance_line(entry: MemoryEntry) -> str:
    """录像带溯源一行（有坐标才产出）：工作空间 + 原文时段。"""
    src = entry.source
    if not src.workspace or (src.tape_start is None and src.tape_end is None):
        return ""

    def _fmt(ts: float | None) -> str:
        if ts is None:
            return "…"
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")

    span = f"{_fmt(src.tape_start)} → {_fmt(src.tape_end)}"
    return f"- 原文：{Path(src.workspace).name} 录像 {span}（record(action=source, id={entry.id}) 回放）"


def format_memory_results(entries: list[MemoryEntry], *, query: str = "") -> str:
    """Wrap recall hits for the model / user."""
    if not entries:
        q = f"（查询：{query}）" if query else ""
        return f"未找到相关记忆{q}。"

    blocks: list[str] = ["<memory_context>", "以下是相关记忆，供参考：", ""]
    for i, entry in enumerate(entries, 1):
        created = entry.created_at.isoformat(timespec="seconds") if entry.created_at else ""
        tags = ", ".join(entry.tags) if entry.tags else "—"
        body = (entry.content or "").strip()
        if len(body) > 1200:
            body = body[:1200] + "…"
        blocks.append(f"### [{i}] {entry.title or entry.id}")
        blocks.append(f"- id: `{entry.id}` · type: `{entry.type}` · status: `{entry.status}` · {created}")
        blocks.append(f"- tags: {tags}")
        provenance = _provenance_line(entry)
        if provenance:
            blocks.append(provenance)
        blocks.append("")
        blocks.append(body)
        blocks.append("")
    blocks.append("</memory_context>")
    return "\n".join(blocks)


def format_memory_show(entry: MemoryEntry) -> str:
    """Format a single memory for show/list detail."""
    created = entry.created_at.isoformat(timespec="seconds") if entry.created_at else ""
    lines = [
        f"# {entry.title or entry.id}",
        "",
        f"- id: `{entry.id}`",
        f"- type: `{entry.type}` · scope: `{entry.scope}` · status: `{entry.status}`",
        f"- sensitivity: `{entry.sensitivity}` · source_type: `{entry.source_type}`",
        f"- created_at: {created}",
        f"- tags: {', '.join(entry.tags) if entry.tags else '—'}",
        f"- access_count: {entry.access_count}",
    ]
    provenance = _provenance_line(entry)
    if provenance:
        lines.append(provenance)
    lines += [
        "",
        entry.content.strip() if entry.content else "（无正文）",
    ]
    return "\n".join(lines)

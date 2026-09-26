"""Read-only aggregation over workspace error logs (errors.jsonl + archives).

错误库分两处：当前写入位置跟随 coara Home（与 coara.log 同目录），
历史遗留位置在 ``<workspace>/.coara/logs``。查询同时覆盖两处，写入只走前者。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.error_log import resolve_error_log_path, resolve_workspace_errors_log_path
from src.core.time import parse_iso_to_datetime

_KINDS_SHOWN = 12
_TOOLS_SHOWN = 12
_EVENTS_SHOWN = 10
_RECENT_MESSAGE_CHARS = 140


def resolve_error_log_paths(workspace_dir: Path, *, coara_home: Path | None = None) -> list[Path]:
    """可读的错误日志文件：当前写入位置 + 归档 + 历史遗留位置（存在才含）。"""
    bases: list[Path] = []
    for path in (
        resolve_error_log_path(workspace_dir, coara_home=coara_home),
        resolve_workspace_errors_log_path(workspace_dir),
    ):
        if path not in bases:
            bases.append(path)

    files: list[Path] = []
    for base in bases:
        if base.is_file():
            files.append(base)
        for archive in sorted(base.parent.glob(f"{base.name}.*")):
            if archive.is_file():
                files.append(archive)
    return files


def iter_error_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """逐行读取错误记录，坏行与不可读文件直接跳过。"""
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError:
            continue


@dataclass(slots=True)
class ErrorSummary:
    paths: list[Path] = field(default_factory=list)
    days: int = 7
    total: int = 0
    skipped_old: int = 0
    first_ts: str = ""
    last_ts: str = ""
    kinds: Counter[str] = field(default_factory=Counter)
    events: Counter[str] = field(default_factory=Counter)
    tools: Counter[str] = field(default_factory=Counter)
    sessions: Counter[str] = field(default_factory=Counter)
    recent: list[dict[str, Any]] = field(default_factory=list)


def summarize_errors(
    *,
    workspace_dir: Path,
    coara_home: Path | None = None,
    days: int = 7,
    kind: str | None = None,
    tool: str | None = None,
    session: str | None = None,
    limit: int = 20,
    paths: Iterable[Path] | None = None,
    now: datetime | None = None,
) -> ErrorSummary:
    """聚合错误记录：按 error_kind / 工具 / 来源 / 会话计数，并保留最近若干条。"""
    files = list(paths) if paths is not None else resolve_error_log_paths(workspace_dir, coara_home=coara_home)
    summary = ErrorSummary(paths=files, days=days)
    cutoff = (now or datetime.now()) - timedelta(days=days) if days > 0 else None

    matched: list[dict[str, Any]] = []
    for record in iter_error_records(files):
        ts = str(record.get("ts") or "")
        moment = parse_iso_to_datetime(ts)
        if cutoff is not None and moment is not None and moment < cutoff:
            summary.skipped_old += 1
            continue
        if kind and str(record.get("error_kind") or "") != kind:
            continue
        if tool and str(record.get("tool") or "") != tool:
            continue
        if session and str(record.get("session_id") or "") != session:
            continue

        summary.total += 1
        if ts:
            if not summary.first_ts or ts < summary.first_ts:
                summary.first_ts = ts
            if ts > summary.last_ts:
                summary.last_ts = ts
        summary.kinds[str(record.get("error_kind") or "unknown")] += 1
        summary.events[str(record.get("source_event") or "unknown")] += 1
        if record.get("tool"):
            summary.tools[str(record["tool"])] += 1
        if record.get("session_id"):
            summary.sessions[str(record["session_id"])] += 1
        matched.append(record)

    summary.recent = matched[-limit:] if limit > 0 else matched
    return summary


def format_error_summary_text(summary: ErrorSummary) -> str:
    """人读摘要：窗口、总量、分布与最近条目。"""
    window = f"最近 {summary.days} 天" if summary.days > 0 else "全部"
    lines = [f"错误日志 · {window} · 共 {summary.total} 条"]
    if summary.skipped_old:
        lines.append(f"（窗口外另有 {summary.skipped_old} 条未统计）")
    if summary.total == 0:
        lines.append("没有记录到错误。")
        return "\n".join(lines)

    lines.append(f"时间范围  {summary.first_ts or '-'} → {summary.last_ts or '-'}")
    lines.append("")
    lines.append("按类型")
    for name, count in summary.kinds.most_common(_KINDS_SHOWN):
        lines.append(f"  {name}  {count}")
    if summary.tools:
        lines.append("")
        lines.append("按工具")
        for name, count in summary.tools.most_common(_TOOLS_SHOWN):
            lines.append(f"  {name}  {count}")
    if summary.events:
        lines.append("")
        lines.append("按来源")
        for name, count in summary.events.most_common(_EVENTS_SHOWN):
            lines.append(f"  {name}  {count}")
    if summary.recent:
        lines.append("")
        lines.append(f"最近 {len(summary.recent)} 条")
        for record in summary.recent:
            lines.append(f"  {_format_recent_line(record)}")
    return "\n".join(lines)


def summary_to_dict(summary: ErrorSummary) -> dict[str, Any]:
    """机器可读形态（--json）。"""
    return {
        "days": summary.days,
        "total": summary.total,
        "skipped_old": summary.skipped_old,
        "first_ts": summary.first_ts,
        "last_ts": summary.last_ts,
        "files": [str(path) for path in summary.paths],
        "kinds": dict(summary.kinds),
        "tools": dict(summary.tools),
        "events": dict(summary.events),
        "sessions": dict(summary.sessions),
        "recent": summary.recent,
    }


def _format_recent_line(record: dict[str, Any]) -> str:
    ts = str(record.get("ts") or "")
    stamp = ts[11:19] if len(ts) >= 19 else ts
    kind = str(record.get("error_kind") or "unknown")
    tool = record.get("tool")
    label = f"{kind}/{tool}" if tool else kind
    message = " ".join(str(record.get("message") or "").split())
    if len(message) > _RECENT_MESSAGE_CHARS:
        message = message[:_RECENT_MESSAGE_CHARS] + "…"
    return f"{stamp}  {label}  {message}"


__all__ = [
    "ErrorSummary",
    "format_error_summary_text",
    "iter_error_records",
    "resolve_error_log_paths",
    "summarize_errors",
    "summary_to_dict",
]

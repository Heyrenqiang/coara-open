"""录像带区间回放：按时间窗切一段对话原文（record 溯源的消费口）。

从 append-only 的 session_events 带滤出时间窗内的事件，投影为可读对话文本。
窗口两侧可加缓冲（记录描述的内容往往略早于/略晚于落笔时刻）；输出带
大小上限，超出时保留尾部（最近内容通常最相关）并标注前段略去。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.session_log.conversation_projection import project_events
from src.session_log.store import iter_events, resolve_session_log_path

_DEFAULT_BUFFER_SECONDS = 300.0
_DEFAULT_MAX_CHARS = 20000


def _ts(event: dict[str, Any]) -> float:
    try:
        return float(event.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hhmm(ts_iso: str) -> str:
    # projection 给的是 ISO（UTC）；就地转回本地 HH:MM 可读性更好
    try:
        dt = datetime.fromisoformat(ts_iso)
        return dt.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return ts_iso


def replay_dialogue(
    workspace_dir: str | Path,
    *,
    ts_start: float | None = None,
    ts_end: float | None = None,
    buffer_seconds: float = _DEFAULT_BUFFER_SECONDS,
    max_chars: int = _DEFAULT_MAX_CHARS,
    coara_home: Path | None = None,
    include_subagents: bool = False,
) -> str:
    """回放工作空间录像带在 [ts_start, ts_end]（含缓冲）内的对话原文。

    两个边界都给 None 返回空串（调用方应至少给一端）；带不存在/窗内无
    内容时返回说明文字，不抛错——回放是尽力而为的派生视图。
    """
    if ts_start is None and ts_end is None:
        return ""
    # 单边界 = 时点窗：以该点为中心向两侧加缓冲；双边界 = 区间窗
    anchor = ts_start if ts_start is not None else ts_end
    low = (ts_start - buffer_seconds) if ts_start is not None else (anchor - buffer_seconds)
    high = (ts_end + buffer_seconds) if ts_end is not None else (anchor + buffer_seconds)

    tape = resolve_session_log_path(workspace_dir, coara_home=coara_home)
    if not tape.is_file() and not list(tape.parent.glob(f"{tape.stem}.20*.jsonl*")):
        return "（该工作空间暂无录像带）"

    events = [e for e in iter_events(tape) if low <= _ts(e) <= high]
    if not events:
        return "（该时段录像带内无对话）"
    events.sort(key=lambda e: int(e.get("seq") or 0))

    exclude = None if include_subagents else frozenset({"subagent"})
    rows = project_events(events, exclude_agent_kinds=exclude)
    if not rows:
        return "（该时段录像带内无对话）"

    lines: list[str] = []
    for row in rows:
        who = "用户" if row["role"] == "user" else str(row.get("coara_name") or "助手")
        content = str(row.get("content") or "").strip()
        if not content and row.get("files"):
            names = [str(f.get("name") or f.get("path") or "?") for f in row["files"]]
            content = f"[发送文件：{'、'.join(names)}]"
        if not content:
            continue
        lines.append(f"[{_hhmm(row['timestamp'])}] {who}：{content}")
    if not lines:
        return "（该时段录像带内无对话）"

    body = "\n\n".join(lines)
    if len(body) > max_chars:
        body = "（前段略）…\n\n" + body[-max_chars:]
    return body


__all__ = ["replay_dialogue"]

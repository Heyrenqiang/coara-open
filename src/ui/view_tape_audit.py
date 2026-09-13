"""录像带完整性自检：每条线的帧数、来源端、回合闭合与序号连续性。

录像带是内核的对话存档，写入是全端的、读取是权威的——它一旦残缺，下游的快照、
对账与回放全错。所以它必须随时可审计：缺口、未闭合回合、来源端缺失、序号漂移
都要查得出来。

只读，不改任何文件。
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


def iter_view_lines(coara_home: Path) -> list[Path]:
    """所有空间的视图线（主会话 conversation.jsonl 与模块会话分文件）。"""
    lines: list[Path] = []
    workspaces = coara_home / "workspaces"
    if workspaces.is_dir():
        lines.extend(sorted(workspaces.glob("*/web_views/*.jsonl")))
    return lines


def audit_view_line(path: Path, *, max_gap_samples: int = 5) -> dict[str, Any]:
    """审计一条线：序号连续性、回合闭合、来源端分布、时间跨度。"""
    seqs: list[int] = []
    kinds: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    started: set[str] = set()
    ended: set[str] = set()
    first_ts: float | None = None
    last_ts: float | None = None
    bad_lines = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except Exception:
                bad_lines += 1
                continue
            seq = int(rec.get("view_seq") or 0)
            if seq:
                seqs.append(seq)
            kind = str(rec.get("kind") or "")
            kinds[kind] += 1
            sources[str(rec.get("source") or "")] += 1
            turn_id = str(rec.get("turn_id") or "")
            if kind == "turn_start" and turn_id:
                started.add(turn_id)
            elif kind == "turn_end" and turn_id:
                ended.add(turn_id)
            ts = rec.get("ts")
            if isinstance(ts, (int, float)):
                first_ts = float(ts) if first_ts is None else min(first_ts, float(ts))
                last_ts = float(ts) if last_ts is None else max(last_ts, float(ts))

    ordered = sorted(seqs)
    gaps = [(prev, cur) for prev, cur in zip(ordered, ordered[1:], strict=False) if cur - prev > 1]
    return {
        "path": str(path),
        "frames": sum(kinds.values()),
        "bad_lines": bad_lines,
        "view_seq_min": ordered[0] if ordered else 0,
        "view_seq_max": ordered[-1] if ordered else 0,
        "seq_gap_count": len(gaps),
        "seq_gaps": gaps[:max_gap_samples],
        "kinds": dict(kinds.most_common()),
        "sources": dict(sources.most_common()),
        "turns_started": len(started),
        "turns_ended": len(ended),
        "open_turns": len(started - ended),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def default_coara_home() -> Path:
    """coara_home 解析：环境变量优先，其次按 workspace_dir 推断，最后 cwd/.coara。"""
    env = os.environ.get("COARA_HOME")
    if env:
        return Path(env)
    try:
        from src.core.coara_home import resolve_coara_home

        return Path(resolve_coara_home(Path.cwd()))
    except Exception:  # noqa: BLE001 — 自检不该因为解析失败就跑不了
        return Path.cwd() / ".coara"


def audit_all(coara_home: Path | None = None) -> list[dict[str, Any]]:
    home = Path(coara_home) if coara_home else default_coara_home()
    return [audit_view_line(path) for path in iter_view_lines(home)]

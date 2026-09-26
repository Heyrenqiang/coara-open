#!/usr/bin/env python3
"""Analyze coara turn latency from trace JSONL. Usage: scripts/dev/README.md"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Allow running from repo root without install
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.coara.workspace_runtime import ActiveWorkspaceRuntime, is_pid_alive, load_active_runtime
from src.core.coara_home import (
    CoaraHomePaths,
    _iter_coara_home_env_values,
    resolve_bootstrap_coara_home,
    resolve_coara_home,
    resolve_config_home,
    resolve_trace_data_dir,
)
from src.core.time import parse_utc_datetime


@dataclass
class TurnSummary:
    turn_id: str = ""
    session_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    user_input: str = ""
    assistant_output: str = ""
    total_ms: float = 0.0
    input_prep_ms: float = 0.0
    context_prep_ms: float = 0.0
    compression_ms: float = 0.0
    llm_ms: float = 0.0
    tools_ms: float = 0.0
    overhead_ms: float = 0.0
    tool_count: int = 0
    iteration_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    iterations: list[dict] = field(default_factory=list)
    source: str = "inferred"


@dataclass
class SessionBlock:
    session_id: str
    started_at: str = ""
    ended_at: str = ""
    ended_reason: str = ""
    turns: list[TurnSummary] = field(default_factory=list)


def _configure_stdout_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            sys.stdout.reconfigure(encoding="utf-8")


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parse_utc_datetime(value).timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class TraceSource:
    workspace_dir: str
    workspace_id: str
    trace_path: Path
    data_dir: Path
    file_mtime: float = 0.0
    file_size: int = 0
    last_event_ts: str = ""
    turn_count: int = 0
    timing_count: int = 0


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _event_row(row: dict) -> tuple[str, dict, str | None]:
    """Normalize trace row → (event_type, payload, timestamp)."""
    if "event_type" in row:
        return str(row["event_type"]), dict(row.get("payload") or {}), row.get("timestamp")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    if isinstance(payload, dict) and "event_type" in payload:
        return str(payload["event_type"]), dict(payload.get("payload") or {}), payload.get("timestamp")
    return "", {}, row.get("timestamp")


def _one_line(text: str, *, max_len: int = 100) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "…"


def _apply_turn_timing(summary: TurnSummary, payload: dict) -> None:
    summary.turn_id = str(payload.get("turn_id", summary.turn_id))
    summary.source = "turn_timing"
    summary.total_ms = float(payload.get("total_ms", summary.total_ms))
    summary.input_prep_ms = float(payload.get("input_prep_ms", 0))
    summary.overhead_ms = float(payload.get("overhead_ms", 0))
    summary.context_prep_ms = 0.0
    summary.compression_ms = 0.0
    summary.llm_ms = 0.0
    summary.tools_ms = 0.0
    summary.tool_count = 0
    summary.iteration_count = 0
    summary.tool_names = []
    summary.iterations = list(payload.get("iterations") or [])
    for item in summary.iterations:
        summary.iteration_count += 1
        summary.context_prep_ms += float(item.get("context_prep_ms", 0))
        summary.compression_ms += float(item.get("compression_ms", 0))
        summary.llm_ms += float(item.get("llm_ms", 0))
        summary.tools_ms += float(item.get("tools_ms", 0))
        tools = item.get("tools") or []
        summary.tool_count += len(tools)
        for tool in tools:
            name = str(tool.get("name", "")).strip()
            if name:
                summary.tool_names.append(name)


def _sync_inferred_iteration_totals(turn: TurnSummary) -> None:
    """Roll up per-iteration inferred timings to turn-level totals."""
    if turn.source == "turn_timing" or not turn.iterations:
        return
    turn.context_prep_ms = sum(float(item.get("context_prep_ms", 0)) for item in turn.iterations)
    turn.compression_ms = sum(float(item.get("compression_ms", 0)) for item in turn.iterations)
    turn.llm_ms = sum(float(item.get("llm_ms", 0)) for item in turn.iterations)
    turn.tools_ms = sum(float(item.get("tools_ms", 0)) for item in turn.iterations)
    turn.tool_count = sum(len(item.get("tools") or []) for item in turn.iterations)
    turn.iteration_count = len(turn.iterations)
    turn.tool_names = []
    for item in turn.iterations:
        for tool in item.get("tools") or []:
            name = str(tool.get("name", "")).strip()
            if name:
                turn.tool_names.append(name)


def _finalize_attributed_overhead(turn: TurnSummary) -> None:
    _sync_inferred_iteration_totals(turn)
    attributed = turn.input_prep_ms + turn.context_prep_ms + turn.compression_ms + turn.llm_ms + turn.tools_ms
    if turn.total_ms > 0:
        turn.overhead_ms = max(0.0, turn.total_ms - attributed)


def _infer_turns(events: list[dict]) -> list[TurnSummary]:
    """Build turn summaries from trace events."""
    turns: list[TurnSummary] = []
    current: TurnSummary | None = None
    pending: TurnSummary | None = None
    iter_context_start: float | None = None
    iter_llm_start: float | None = None
    iter_tools_start: float | None = None
    compression_start: float | None = None
    current_iter: dict | None = None

    def _open_infer_iteration() -> dict:
        nonlocal current_iter
        iteration_index = len(current.iterations) + 1 if current else 1
        current_iter = {
            "iteration": iteration_index,
            "context_prep_ms": 0.0,
            "compression_ms": 0.0,
            "llm_ms": 0.0,
            "tools_ms": 0.0,
            "tools": [],
        }
        if current is not None:
            current.iterations.append(current_iter)
        return current_iter

    def flush_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        _finalize_attributed_overhead(pending)
        turns.append(pending)
        pending = None

    for row in events:
        event_type, payload, ts_raw = _event_row(row)
        ts = _parse_ts(ts_raw)
        if not event_type:
            continue

        session_id = str(payload.get("session_id") or "")

        if event_type == "turn_timing":
            if ts_raw:
                if pending is not None:
                    pending.ended_at = pending.ended_at or ts_raw
                elif current is not None:
                    current.ended_at = current.ended_at or ts_raw
            target = pending or current
            if target is None:
                target = TurnSummary(started_at=ts_raw or "", session_id=session_id)
                pending = target
            _apply_turn_timing(target, payload)
            if target is pending:
                flush_pending()
            else:
                _finalize_attributed_overhead(target)
                turns.append(target)
                current = None
            iter_context_start = None
            iter_llm_start = None
            iter_tools_start = None
            compression_start = None
            current_iter = None
            continue

        if event_type == "conversation_message" and payload.get("role") == "user":
            flush_pending()
            if current is not None:
                _finalize_attributed_overhead(current)
                turns.append(current)
            content = str(payload.get("content", ""))
            current = TurnSummary(
                started_at=ts_raw or "",
                session_id=session_id,
                user_input=content,
            )
            current_iter = None
            iter_context_start = ts
            continue

        if current is None:
            continue

        if session_id and not current.session_id:
            current.session_id = session_id

        if event_type == "final_response":
            preview = str(payload.get("content_preview") or payload.get("content") or "")
            if preview:
                current.assistant_output = preview
            continue

        if event_type == "conversation_message" and payload.get("role") == "assistant":
            content = str(payload.get("content", ""))
            if content and not current.assistant_output:
                current.assistant_output = content
            continue

        if ts is None:
            continue

        if event_type == "llm_turn_start":
            if current_iter is not None and iter_context_start is not None:
                current_iter["context_prep_ms"] += max(0.0, (ts - iter_context_start) * 1000)
            current_iter = _open_infer_iteration()
            iter_llm_start = ts
            iter_context_start = None
            current.iteration_count += 1
            continue

        if event_type == "context_compressed":
            compression_start = ts
            continue

        if event_type == "llm_turn_complete":
            if compression_start is not None and iter_llm_start is not None:
                delta = max(0.0, (iter_llm_start - compression_start) * 1000)
                if current_iter is not None:
                    current_iter["compression_ms"] += delta
                else:
                    current.compression_ms += delta
                compression_start = None
            if iter_llm_start is not None:
                delta = max(0.0, (ts - iter_llm_start) * 1000)
                if current_iter is not None:
                    current_iter["llm_ms"] += delta
                else:
                    current.llm_ms += delta
                iter_llm_start = None
            continue

        if event_type in {"tool_call", "tool_start"}:
            tool_name = str(payload.get("tool_name") or payload.get("name") or "").strip()
            if tool_name and tool_name not in current.tool_names:
                current.tool_names.append(tool_name)
            if event_type == "tool_call":
                if iter_tools_start is None:
                    iter_tools_start = ts
                current.tool_count += 1
            continue

        if event_type == "tool_complete":
            tool_name = str(payload.get("tool_name") or payload.get("name") or "").strip()
            duration = payload.get("duration_ms")
            duration_value: float | None = None
            if isinstance(duration, (int, float)):
                duration_value = float(duration)
                current.tools_ms += duration_value
            elif iter_tools_start is not None:
                duration_value = max(0.0, (ts - iter_tools_start) * 1000)
                current.tools_ms += duration_value
            if current_iter is not None:
                if duration_value is not None:
                    current_iter["tools_ms"] += duration_value
                current_iter["tools"].append(
                    {
                        "name": tool_name or "?",
                        "duration_ms": duration_value,
                        "is_error": bool(payload.get("is_error")),
                    }
                )
            iter_tools_start = None
            iter_context_start = ts
            continue

        if event_type in {"completed", "turn_failed", "turn_interrupt_requested"}:
            if current.started_at:
                start = _parse_ts(current.started_at)
                if start is not None:
                    current.total_ms = max(0.0, (ts - start) * 1000)
            current.ended_at = ts_raw or current.ended_at
            pending = current
            current = None
            iter_context_start = None
            iter_llm_start = None
            iter_tools_start = None
            compression_start = None
            current_iter = None
            continue

    flush_pending()
    if current is not None:
        _finalize_attributed_overhead(current)
        turns.append(current)
    return turns


def _group_sessions(turns: list[TurnSummary], events: list[dict]) -> list[SessionBlock]:
    if not turns:
        return []

    shutdown_times: dict[str, str] = {}
    for row in events:
        event_type, payload, ts_raw = _event_row(row)
        if event_type == "shutdown" and ts_raw:
            session_id = str(payload.get("session_id") or "")
            if session_id:
                shutdown_times[session_id] = ts_raw

    sessions: list[SessionBlock] = []
    current: SessionBlock | None = None
    for turn in turns:
        sid = turn.session_id or "unknown"
        if current is None or current.session_id != sid:
            if current is not None:
                sessions.append(current)
            current = SessionBlock(session_id=sid, started_at=turn.started_at)
        if not current.started_at:
            current.started_at = turn.started_at
        current.turns.append(turn)
        if turn.ended_at:
            current.ended_at = turn.ended_at
    if current is not None:
        sessions.append(current)

    for session in sessions:
        if session.session_id in shutdown_times:
            session.ended_at = shutdown_times[session.session_id]
            session.ended_reason = "shutdown"
        elif session.ended_at:
            session.ended_reason = "last_turn"
        else:
            session.ended_reason = "ongoing"
    return sessions


def _filter_turns(turns: list[TurnSummary], grep: str | None) -> list[TurnSummary]:
    if not grep:
        return turns
    needle = grep.casefold()
    filtered: list[TurnSummary] = []
    for turn in turns:
        hay = f"{turn.user_input}\n{turn.assistant_output}".casefold()
        if needle in hay:
            filtered.append(turn)
    return filtered


def _format_ms(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def _short_session_id(session_id: str) -> str:
    if not session_id or session_id == "unknown":
        return "unknown"
    return session_id[:8]


def _timing_is_aggregate_reliable(turn: TurnSummary) -> bool:
    if turn.source == "turn_timing":
        return True
    if turn.total_ms <= 0:
        return False
    attributed = turn.input_prep_ms + turn.context_prep_ms + turn.compression_ms + turn.llm_ms + turn.tools_ms
    if attributed > turn.total_ms * 2.5:
        return False
    return bool(turn.iterations)


def _print_timing_line(turn: TurnSummary) -> None:
    parts = [
        f"total {_format_ms(turn.total_ms)}",
        f"llm {_format_ms(turn.llm_ms)}",
        f"tools {_format_ms(turn.tools_ms)} ({turn.tool_count})",
        f"context {_format_ms(turn.context_prep_ms)}",
    ]
    if turn.compression_ms:
        parts.append(f"compress {_format_ms(turn.compression_ms)}")
    parts.append(f"overhead {_format_ms(turn.overhead_ms)}")
    print(f"    ⏱  {' | '.join(parts)}  [{turn.source}, {turn.iteration_count} iters]")

    if turn.source == "inferred":
        print(
            "    △  inferred from trace events — incomplete turns and parallel tools "
            "may skew totals; prefer [turn_timing] rows for profiling"
        )

    if turn.iterations:
        for item in turn.iterations:
            idx = item.get("iteration", "?")
            ctx = float(item.get("context_prep_ms", 0))
            compress = float(item.get("compression_ms", 0))
            llm = float(item.get("llm_ms", 0))
            tools_total = float(item.get("tools_ms", 0))
            header_parts = [f"context {_format_ms(ctx)}"]
            if compress:
                header_parts.append(f"compress {_format_ms(compress)}")
            print(f"       iter {idx}: {' | '.join(header_parts)}")
            print(f"         llm    {_format_ms(llm)}")

            tools = item.get("tools") or []
            if tools:
                for tool in tools:
                    name = str(tool.get("name") or "?")
                    duration = tool.get("duration_ms")
                    err_flag = " (err)" if tool.get("is_error") else ""
                    if isinstance(duration, (int, float)):
                        print(f"         tool   {name:<18} {_format_ms(float(duration))}{err_flag}")
                    else:
                        print(f"         tool   {name:<18} ?{err_flag}")
                tool_sum = sum(
                    float(t.get("duration_ms")) for t in tools if isinstance(t.get("duration_ms"), (int, float))
                )
                if tools_total > tool_sum + 50:
                    print(
                        f"         note   batch wall {_format_ms(tools_total)} "
                        f"(parallel overlap vs sum {_format_ms(tool_sum)})"
                    )
            elif tools_total:
                print(f"         tools  {_format_ms(tools_total)} (no per-tool names recorded)")
            else:
                print("         tools  —")
    elif turn.tool_names:
        shown = ", ".join(turn.tool_names[:12])
        if len(turn.tool_names) > 12:
            shown += f", +{len(turn.tool_names) - 12} more"
        print(f"    ⚙ {shown}")


def _print_turn(turn: TurnSummary, index: int, *, preview_len: int) -> None:
    started = turn.started_at or "?"
    print(f"\n  Turn {index}  {started}  ({_format_ms(turn.total_ms)})")
    print(f"    USER   {_one_line(turn.user_input, max_len=preview_len)}")
    _print_timing_line(turn)
    if turn.assistant_output:
        print(f"    COARA  {_one_line(turn.assistant_output, max_len=preview_len)}")
    else:
        print("    COARA  (no final response recorded)")


def _print_session(session: SessionBlock, start_index: int, *, preview_len: int) -> int:
    sid = _short_session_id(session.session_id)
    started = session.started_at or "?"
    print(f"\n{'═' * 72}")
    print(f"Session {sid}…  started {started}")
    idx = start_index
    for turn in session.turns:
        _print_turn(turn, idx, preview_len=preview_len)
        idx += 1
    end_note = session.ended_at or "?"
    reason = {
        "shutdown": "process exited",
        "last_turn": "last recorded turn",
        "ongoing": "still open",
    }.get(session.ended_reason, session.ended_reason)
    print(f"{'─' * 72}")
    print(f"Session {sid}… end  {len(session.turns)} turn(s) | {end_note} | {reason}")
    return idx


def _print_aggregate(turns: list[TurnSummary]) -> None:
    if not turns:
        return
    reliable = [turn for turn in turns if _timing_is_aggregate_reliable(turn)]
    skipped = len(turns) - len(reliable)
    if not reliable:
        print(f"\n{'═' * 72}")
        print("Aggregate skipped — no turns with reliable turn_timing breakdown in this view.")
        if skipped:
            print(f"  ({skipped} turn(s) omitted: inferred or incomplete)")
        return
    totals = defaultdict(float)
    for turn in reliable:
        totals["total"] += turn.total_ms
        totals["llm"] += turn.llm_ms
        totals["tools"] += turn.tools_ms
        totals["context_prep"] += turn.context_prep_ms
        totals["compression"] += turn.compression_ms
        totals["overhead"] += turn.overhead_ms
    count = len(reliable)
    print(f"\n{'═' * 72}")
    title = f"Aggregate ({count} turn(s), average per turn)"
    if skipped:
        title += f" — {skipped} unreliable turn(s) excluded"
    print(title)
    for key in ("total", "llm", "tools", "context_prep", "compression", "overhead"):
        avg = totals[key] / count
        pct = (totals[key] / totals["total"] * 100) if totals["total"] else 0
        print(f"  {key:14} {_format_ms(avg):>10}  ({pct:4.1f}% of wall time)")


def resolve_trace_path(workspace: Path, coara_home: Path | None) -> Path:
    data_dir = resolve_trace_data_dir(workspace, configured_home=coara_home)
    return data_dir / "trace_events.jsonl"


def _load_registry_paths(coara_home: Path) -> dict[str, str]:
    """Map workspace_id → absolute path from coara Home registry."""
    registry_file = coara_home / "registry" / "workspaces.yaml"
    if not registry_file.exists():
        return {}
    try:
        import yaml

        payload = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    workspaces = payload.get("workspaces") or {}
    for item in workspaces.values():
        if not isinstance(item, dict):
            continue
        workspace_id = str(item.get("id") or "").strip()
        path = str(item.get("path") or "").strip()
        if workspace_id and path:
            mapping[workspace_id] = path
    return mapping


def _summarize_trace_source(
    *,
    workspace_dir: str,
    workspace_id: str,
    trace_path: Path,
    data_dir: Path,
) -> TraceSource | None:
    if not trace_path.exists():
        return None
    stat = trace_path.stat()
    events = _load_jsonl(trace_path)
    turns = _infer_turns(events)
    last_ts = ""
    if events:
        _, _, last_ts = _event_row(events[-1])
        last_ts = last_ts or ""
    timing_count = sum(1 for turn in turns if turn.source == "turn_timing")
    return TraceSource(
        workspace_dir=workspace_dir,
        workspace_id=workspace_id,
        trace_path=trace_path,
        data_dir=data_dir,
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        last_event_ts=last_ts,
        turn_count=len(turns),
        timing_count=timing_count,
    )


def discover_trace_sources(
    *,
    coara_home: Path | None = None,
    workspace: Path | None = None,
    coara_homes: list[Path] | None = None,
) -> list[TraceSource]:
    """Find all trace_events.jsonl files relevant to this install."""
    cwd = (workspace or Path.cwd()).expanduser().resolve()
    homes = coara_homes or _coara_home_candidates(explicit=coara_home, cwd=cwd)
    found: dict[str, TraceSource] = {}

    for home in homes:
        registry = _load_registry_paths(home)
        container = home / "workspaces"
        if not container.is_dir():
            continue
        for entry in sorted(container.iterdir()):
            if not entry.is_dir():
                continue
            workspace_id = entry.name
            trace_path = entry / "traces" / "trace_events.jsonl"
            key = str(trace_path.resolve())
            if key in found:
                continue
            workspace_dir = registry.get(workspace_id, f"(unregistered, id={workspace_id})")
            source = _summarize_trace_source(
                workspace_dir=workspace_dir,
                workspace_id=workspace_id,
                trace_path=trace_path,
                data_dir=trace_path.parent,
            )
            if source is not None:
                found[key] = source

    return sorted(found.values(), key=lambda item: item.file_mtime, reverse=True)


def _format_mtime(ts: float) -> str:
    if ts <= 0:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _coara_home_from_yaml(home: Path) -> Path | None:
    """Read ``coara_home:`` from ``<home>/config/config.yaml`` when present."""
    config_file = home / "config" / "config.yaml"
    if not config_file.is_file():
        return None
    try:
        import yaml

        cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        raw_home = cfg.get("coara_home")
        if raw_home:
            return Path(str(raw_home)).expanduser()
    except Exception:
        return None
    return None


def _resolve_configured_coara_home(*, cwd: Path | None = None) -> Path | None:
    """Match runtime config resolution: bootstrap home with providers, then env."""
    bootstrap = resolve_bootstrap_coara_home()
    if bootstrap is not None:
        return bootstrap
    try:
        return resolve_config_home()
    except Exception:
        pass
    workspace = (cwd or Path.cwd()).expanduser().resolve()
    return resolve_coara_home(workspace, None)


def _coara_home_candidates(*, explicit: Path | None, cwd: Path) -> list[Path]:
    """All coara Home directories worth scanning (same sources as the runtime)."""
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(value: Path | str | None) -> None:
        if value is None:
            return
        try:
            resolved = Path(value).expanduser().resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        ordered.append(resolved)

    add(explicit)
    add(resolve_bootstrap_coara_home())
    with contextlib.suppress(Exception):
        add(resolve_config_home())
    for raw in _iter_coara_home_env_values():
        add(raw)
    # coara_home inside each candidate's machine config (e.g. D:/coara under D:\coara\config)
    for home in list(ordered):
        add(_coara_home_from_yaml(home))
    config_file = _REPO / "config.yaml"
    if config_file.exists():
        try:
            import yaml

            cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
            raw_home = cfg.get("coara_home")
            if raw_home:
                add(Path(str(raw_home)))
        except Exception:
            pass
    local = (cwd / ".coara").resolve()
    if local.is_dir():
        add(local)
    add(resolve_coara_home(cwd, None))
    return ordered


def _resolve_primary_coara_home(*, explicit: Path | None, cwd: Path) -> Path:
    candidates = _coara_home_candidates(explicit=explicit, cwd=cwd)
    for home in candidates:
        if resolve_trace_path(cwd, home).exists():
            return home
    bootstrap = resolve_bootstrap_coara_home()
    if bootstrap is not None:
        return bootstrap
    if candidates:
        return candidates[0]
    return resolve_coara_home(cwd, explicit)


def _alias_for_path(coara_home: Path, workspace_path: Path) -> str | None:
    registry_file = coara_home / "registry" / "workspaces.yaml"
    if not registry_file.exists():
        return None
    try:
        import yaml

        payload = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    target = workspace_path.expanduser().resolve()
    for _workspace_id, item in (payload.get("workspaces") or {}).items():
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path") or "").strip()
        if not raw:
            continue
        try:
            if Path(raw).expanduser().resolve() == target:
                # Return the project's name (preferred) or fall back to id.
                return str(item.get("name") or item.get("id") or "")
        except OSError:
            continue
    return None


def _dedupe_trace_sources(sources: list[TraceSource]) -> list[TraceSource]:
    best: dict[str, TraceSource] = {}
    for source in sources:
        existing = best.get(source.workspace_id)
        if existing is None or source.file_mtime > existing.file_mtime:
            best[source.workspace_id] = source
    return sorted(best.values(), key=lambda item: item.file_mtime, reverse=True)


def _project_label(source: TraceSource) -> str:
    raw = source.workspace_dir
    if raw.startswith("("):
        return source.workspace_id
    return Path(raw).name or source.workspace_id


def _print_overview(
    *,
    coara_home: Path,
    anchor: Path,
    sources: list[TraceSource],
    selected: TraceSource | None,
    text_filter: str | None,
    coara_homes: list[Path] | None = None,
    workspace_override: Path | None = None,
) -> None:
    alias = _alias_for_path(coara_home, anchor)
    print("=" * 72)
    print("coara turn timing")
    print(f"  coara_home:  {coara_home}")
    print(f"  anchor:      {anchor.resolve()}")
    if alias:
        print(f"  registry:    {alias}")
    live = _load_live_active_runtime(coara_homes or [coara_home])
    if live is not None:
        _, runtime = live
        sid = runtime.session_id[:8] + "…" if runtime.session_id else "?"
        print(f"  live:        {runtime.workspace_name or runtime.workspace_id}  session {sid}  pid {runtime.pid}")
    if workspace_override is not None:
        print(f"  project:     {workspace_override.resolve()}  (--workspace)")
    elif selected is not None and live is not None and not selected.workspace_dir.startswith("("):
        selected_path = Path(selected.workspace_dir).expanduser().resolve()
        live_path = Path(live[1].workspace_path).expanduser().resolve()
        anchor_path = anchor.expanduser().resolve()
        if selected_path == live_path and selected_path != anchor_path:
            print(f"  note:        using live project {live[1].workspace_name or live[1].workspace_id}")
    if text_filter:
        print(f"  filter:      {text_filter!r}")
    print()
    if not sources:
        print("No trace files found. Start ``coara`` in this directory first.")
        return
    print("Projects with trace data  (* = report below uses this project's trace):")
    for source in sources:
        marker = " *" if selected and source.trace_path.resolve() == selected.trace_path.resolve() else "  "
        label = _project_label(source)
        timing = "measured" if source.timing_count else "inferred"
        print(f"{marker} {label:<18} {source.turn_count:>5} turns  last {source.last_event_ts or '?'}  ({timing})")
    print()


def _resolve_trace_path_for_cwd(cwd: Path, coara_homes: list[Path]) -> Path:
    for home in coara_homes:
        path = resolve_trace_path(cwd, home)
        if path.exists():
            return path
    return resolve_trace_path(cwd, coara_homes[0] if coara_homes else None)


def _trace_source_for_workspace_path(project_path: Path, sources: list[TraceSource]) -> TraceSource | None:
    target = project_path.expanduser().resolve()
    for source in sources:
        raw = source.workspace_dir
        if raw.startswith("("):
            continue
        try:
            if Path(raw).expanduser().resolve() == target:
                return source
        except OSError:
            continue
    return None


def _load_live_active_runtime(coara_homes: list[Path]) -> tuple[Path, ActiveWorkspaceRuntime] | None:
    for home in coara_homes:
        runtime = load_active_runtime(home)
        if runtime is None or not is_pid_alive(runtime.pid):
            continue
        return home, runtime
    return None


def _select_trace_source(
    *,
    anchor: Path,
    coara_homes: list[Path],
    all_sources: list[TraceSource],
    trace_file: Path | None,
    workspace: Path | None,
    prefer_fresh: bool,
) -> tuple[Path, TraceSource | None]:
    deduped = _dedupe_trace_sources(all_sources)
    if trace_file is not None:
        path = trace_file.expanduser().resolve()
        source = next((item for item in deduped if item.trace_path.resolve() == path), None)
        return path, source

    if prefer_fresh and deduped:
        return deduped[0].trace_path, deduped[0]

    if workspace is not None:
        source = _trace_source_for_workspace_path(workspace, deduped)
        if source is not None:
            return source.trace_path, source

    live = _load_live_active_runtime(coara_homes)
    if live is not None:
        _, runtime = live
        active_source = _trace_source_for_workspace_path(Path(runtime.workspace_path), deduped)
        if active_source is not None:
            return active_source.trace_path, active_source

    path = _resolve_trace_path_for_cwd(anchor, coara_homes)
    source = next((item for item in deduped if item.trace_path.resolve() == path.resolve()), None)
    if source is not None:
        return path, source

    if path.exists():
        primary_home = coara_homes[0] if coara_homes else None
        paths = CoaraHomePaths.for_workspace(anchor, configured_home=primary_home)
        source = _summarize_trace_source(
            workspace_dir=str(anchor.resolve()),
            workspace_id=paths.workspace_id,
            trace_path=path,
            data_dir=path.parent,
        )
        return path, source

    if deduped:
        return deduped[0].trace_path, deduped[0]
    return path, None


def _print_trace_sources(sources: list[TraceSource], *, selected: Path | None = None) -> None:
    print(f"{'Workspace':<34} {'ID':<22} {'Last event':<20} {'Turns':>5}  Trace file")
    print("-" * 120)
    for source in sources:
        marker = " *" if selected and source.trace_path.resolve() == selected.resolve() else "  "
        timing_note = "measured" if source.timing_count else "inferred"
        ws = _one_line(source.workspace_dir, max_len=32)
        print(
            f"{marker}{ws:<34} {source.workspace_id:<22} "
            f"{source.last_event_ts or '?':<20} {source.turn_count:>5}  "
            f"{source.trace_path}"
        )
        print(f"    mtime={_format_mtime(source.file_mtime)} size={source.file_size} timing={timing_note}")
    if selected is None and sources:
        print("\nTip: pass --workspace <dir> or --trace-file <path> to analyze a specific trace.")
        print("     Use --fresh to auto-pick the newest trace under coara_home.")


def _trace_meta(events: list[dict], turns: list[TurnSummary]) -> tuple[str, int, int]:
    last_ts = ""
    if events:
        _, _, last_ts = _event_row(events[-1])
        last_ts = last_ts or ""
    timing_count = sum(1 for turn in turns if turn.source == "turn_timing")
    inferred_count = len(turns) - timing_count
    return last_ts, timing_count, inferred_count


def main() -> int:
    _configure_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="Turn timing report from coara trace JSONL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See scripts/dev/README.md for examples.",
    )
    parser.add_argument(
        "filter",
        nargs="?",
        default=None,
        help="Optional keyword — only show turns whose USER/COARA text contains this",
    )
    parser.add_argument(
        "--last-sessions",
        type=int,
        default=2,
        metavar="N",
        help="How many recent sessions to show (default: 2)",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        metavar="ID",
        help="Only show turns from this session (prefix match ok)",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Analyze a specific project directory (overrides live active.json)",
    )
    parser.add_argument(
        "--trace-file",
        type=Path,
        default=None,
        help="Analyze a specific trace_events.jsonl file",
    )
    parser.add_argument(
        "--list-traces",
        action="store_true",
        help="List discovered trace files and exit",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Use the newest trace file under coara_home",
    )
    parser.add_argument("--coara-home", type=Path, default=None, help="Override coara_home")
    parser.add_argument("--last", type=int, default=None, help="Show only the last N turns")
    parser.add_argument("--preview-len", type=int, default=120, help="Max chars for USER/COARA preview")
    args = parser.parse_args()

    text_filter = args.filter
    anchor = Path.cwd().expanduser().resolve()
    workspace = args.workspace.expanduser().resolve() if args.workspace is not None else None
    coara_homes = _coara_home_candidates(explicit=args.coara_home, cwd=anchor)
    home = _resolve_primary_coara_home(explicit=args.coara_home, cwd=anchor)
    all_sources = discover_trace_sources(coara_homes=coara_homes, workspace=anchor)
    deduped = _dedupe_trace_sources(all_sources)

    if args.list_traces:
        print(f"coara_home: {home}")
        if not deduped:
            print("No trace_events.jsonl files found.")
            return 0
        trace_path, _selected_source = _select_trace_source(
            anchor=anchor,
            coara_homes=coara_homes,
            all_sources=all_sources,
            trace_file=args.trace_file,
            workspace=workspace,
            prefer_fresh=args.fresh,
        )
        _print_trace_sources(deduped, selected=trace_path if trace_path.exists() else None)
        return 0

    trace_path, selected_source = _select_trace_source(
        anchor=anchor,
        coara_homes=coara_homes,
        all_sources=all_sources,
        trace_file=args.trace_file,
        workspace=workspace,
        prefer_fresh=args.fresh,
    )

    _print_overview(
        coara_home=home,
        anchor=anchor,
        sources=deduped,
        selected=selected_source,
        text_filter=text_filter,
        coara_homes=coara_homes,
        workspace_override=workspace,
    )

    if not trace_path.exists():
        if deduped:
            print(f"Trace not found; newest available: {deduped[0].trace_path}", file=sys.stderr)
        else:
            print("Run coara chat first to generate traces.", file=sys.stderr)
        return 1

    events = _load_jsonl(trace_path)
    turns = _infer_turns(events)
    if not turns:
        print(f"No turns in {trace_path}")
        return 0

    file_turn_count = len(turns)
    if text_filter:
        turns = _filter_turns(turns, text_filter)
        if not turns:
            print(f"No turns matched filter {text_filter!r} (searched {file_turn_count} turn(s) in file)")
            return 0

    all_sessions = _group_sessions(_infer_turns(events), events)
    sessions = _group_sessions(turns, events)
    if args.session_id:
        needle = args.session_id.strip().lower()
        sessions = [
            session
            for session in sessions
            if session.session_id.lower().startswith(needle) or needle in session.session_id.lower()
        ]
        if not sessions:
            print(f"No session matched --session-id {args.session_id!r} ({file_turn_count} turn(s) in file)")
            return 0
        turns = [turn for session in sessions for turn in session.turns]

    last_ts, timing_count, inferred_count = _trace_meta(events, turns)

    if args.last is not None:
        selected_turns = turns[-args.last :]
        selected_ids = {id(turn) for turn in selected_turns}
        selected_sessions = []
        for session in sessions:
            session_turns = [turn for turn in session.turns if id(turn) in selected_ids]
            if session_turns:
                selected_sessions.append(
                    SessionBlock(
                        session_id=session.session_id,
                        started_at=session_turns[0].started_at,
                        ended_at=session_turns[-1].ended_at or session.ended_at,
                        ended_reason=session.ended_reason,
                        turns=session_turns,
                    )
                )
        start_index = max(1, len(turns) - len(selected_turns) + 1)
    elif args.session_id:
        selected_sessions = sessions
        selected_turns = turns
        start_index = 1
    else:
        selected_sessions = sessions[-args.last_sessions :]
        selected_turns = [turn for session in selected_sessions for turn in session.turns]
        start_index = sum(len(session.turns) for session in sessions[: len(sessions) - len(selected_sessions)]) + 1

    if selected_source is not None:
        print(f"Analyzing @{_project_label(selected_source)}  ({selected_source.workspace_id})")
    print(f"trace: {trace_path}")
    print(
        f"file: {file_turn_count} turns, {len(all_sessions)} sessions | "
        f"showing {len(selected_turns)} turn(s) in {len(selected_sessions)} session(s) | "
        f"last event {last_ts or '?'}"
    )
    if timing_count == 0:
        print("timing: all inferred (~1s precision)")
    else:
        print(f"timing: {timing_count} measured, {inferred_count} inferred")

    idx = start_index
    for session in selected_sessions:
        idx = _print_session(session, idx, preview_len=args.preview_len)

    _print_aggregate(selected_turns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Unified workspace error log for agent/tool diagnostics.

All runtime errors append to ``<workspace>/.coara/logs/errors.jsonl`` under the active
workspace directory.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from src.core.time import now_iso

_MAX_ARG_CHARS = 16_000
_MAX_MESSAGE_CHARS = 4000
_MAX_TRACEBACK_CHARS = 32_000

# 单文件上限与归档策略：错误量比 coara.log 小得多，但需要留档做问题分析，
# 因此归档份数少、保留期长（coara.log 是 10MB / 7 天）。
_MAX_LOG_BYTES = 8 * 1024 * 1024
_MAX_ARCHIVES = 3
_ARCHIVE_KEEP_DAYS = 30

_lock = threading.Lock()


def resolve_workspace_errors_log_path(workspace_dir: Path) -> Path:
    """Legacy per-workspace error JSONL path (``<workspace>/.coara/logs``)."""
    return Path(workspace_dir).expanduser().resolve() / ".coara" / "logs" / "errors.jsonl"


def resolve_error_log_path(workspace_dir: Path, coara_home: Path | None = None) -> Path:
    """写入位置：跟随 coara Home 的 logs 目录，与 coara.log 同处。

    无全局 home 时 CoaraHomePaths 自动回退到 ``<workspace>/.coara/logs``，
    与旧路径一致；配置了全局 home 时错误日志不再散落在工作区目录里。
    """
    from src.core.coara_home import CoaraHomePaths

    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home=coara_home, migrate=False)
    return paths.logs_dir / "errors.jsonl"


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(path)


def purge_session_audit_logs(*, workspace_dir: Path, coara_home: Path | None = None) -> None:
    """Delete deprecated per-session tool audit directories for one workspace."""
    from src.core.coara_home import CoaraHomePaths

    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home=coara_home, migrate=False)
    legacy_session_dir = paths.logs_dir / "sessions"
    targets = [
        legacy_session_dir,
        Path(workspace_dir).expanduser().resolve() / ".coara" / "logs" / "sessions",
    ]
    coara_home_path = paths.root if paths.uses_global_home else None
    if coara_home_path is not None:
        workspaces_root = coara_home_path / "workspaces"
        if workspaces_root.is_dir():
            for ws_dir in workspaces_root.iterdir():
                if ws_dir.is_dir():
                    targets.append(ws_dir / "logs" / "sessions")
    for target in targets:
        _remove_tree(target)


def classify_tool_error(
    *,
    event: str,
    reason: str | None = None,
    message: str | None = None,
    is_cancelled: bool = False,
) -> str:
    """Map runtime signals to a stable error_kind for aggregation."""
    if is_cancelled:
        return "cancelled"
    if event in {"llm_error", "turn_failed", "turn_interrupt", "agent_error"}:
        return event
    text = " ".join(part for part in (reason or "", message or "") if part).lower()
    if event == "tool_blocked":
        if "not found" in text:
            return "tool_not_found"
        return "blocked"
    if "user" in text and ("cancel" in text or "denied" in text or "deny" in text):
        return "user_denied"
    if "sandbox" in text:
        return "sandbox"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "invalid parameter" in text or "validation" in text:
        return "validation"
    if "plan mode" in text or "计划模式" in text:
        return "plan_mode"
    return "execution"


def _truncate_value(value: Any, *, max_chars: int = _MAX_ARG_CHARS) -> Any:
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"... [truncated {len(value) - max_chars} chars]"
    if isinstance(value, dict):
        return {key: _truncate_value(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_value(item, max_chars=max_chars) for item in value]
    return value


def _compact_text(value: Any, *, max_chars: int) -> tuple[str, bool]:
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, list) else str(value or "").strip()
    if len(text) > max_chars:
        return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]", True
    return text, False


def _rotate_archives(log_path: Path) -> None:
    """归档滚动：删最老一份，其余序号 +1，为当前文件腾出 .1。"""
    oldest = log_path.with_name(f"{log_path.name}.{_MAX_ARCHIVES}")
    with contextlib.suppress(OSError):
        if oldest.exists():
            oldest.unlink()
    for index in range(_MAX_ARCHIVES - 1, 0, -1):
        source = log_path.with_name(f"{log_path.name}.{index}")
        if not source.exists():
            continue
        with contextlib.suppress(OSError):
            source.replace(log_path.with_name(f"{log_path.name}.{index + 1}"))


def _prune_stale_archives(log_path: Path) -> None:
    """删除超过保留期的归档，避免只滚不删。"""
    cutoff = time.time() - _ARCHIVE_KEEP_DAYS * 86400
    for archive in log_path.parent.glob(f"{log_path.name}.*"):
        try:
            if archive.is_file() and archive.stat().st_mtime < cutoff:
                archive.unlink()
        except OSError:
            continue


def _maybe_rotate(log_path: Path) -> None:
    """超过单文件上限即轮转；任何失败都不阻断写入。"""
    try:
        if not log_path.exists() or log_path.stat().st_size < _MAX_LOG_BYTES:
            return
        _rotate_archives(log_path)
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
    except OSError:
        return
    _prune_stale_archives(log_path)


def _append_record(log_path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(log_path)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def log_tool_error_event(
    *,
    workspace_dir: Path,
    session_id: str,
    coara_id: str,
    coara_name: str,
    source_event: str,
    tool_name: str,
    tool_call_id: str,
    message: str,
    arguments: dict[str, Any] | None = None,
    reason: str | None = None,
    duration_ms: float | None = None,
    is_cancelled: bool = False,
    metadata: dict[str, Any] | None = None,
    coara_home: Path | None = None,
) -> Path:
    """Append one tool failure to the workspace error log."""
    body, truncated = _compact_text(message or reason or "tool error", max_chars=_MAX_MESSAGE_CHARS)
    error_kind = classify_tool_error(
        event=source_event,
        reason=reason,
        message=body,
        is_cancelled=is_cancelled,
    )
    record: dict[str, Any] = {
        "ts": now_iso(),
        "kind": "tool_error",
        "source_event": source_event,
        "error_kind": error_kind,
        "session_id": session_id,
        "coara_id": coara_id,
        "coara_name": coara_name,
        "tool": tool_name,
        "tool_call_id": tool_call_id,
        "message": body,
        "is_cancelled": is_cancelled,
    }
    if reason:
        record["reason"] = reason
    if arguments is not None:
        record["arguments"] = _truncate_value(arguments)
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 2)
    if metadata:
        record["metadata"] = _truncate_value(metadata)
    if truncated:
        record["message_truncated"] = True

    log_path = resolve_error_log_path(workspace_dir, coara_home=coara_home)
    _append_record(log_path, record)
    return log_path


def log_session_error_event(
    *,
    workspace_dir: Path,
    session_id: str,
    coara_id: str,
    event: str,
    message: str,
    coara_name: str = "",
    metadata: dict[str, Any] | None = None,
    traceback: str | None = None,
    coara_home: Path | None = None,
) -> Path:
    """Append one non-tool runtime error to the workspace error log."""
    body, msg_truncated = _compact_text(message, max_chars=_MAX_MESSAGE_CHARS)
    error_kind = classify_tool_error(event=event, message=body)
    record: dict[str, Any] = {
        "ts": now_iso(),
        "kind": "session_error",
        "source_event": event,
        "error_kind": error_kind,
        "session_id": session_id,
        "coara_id": coara_id,
        "message": body,
    }
    if coara_name:
        record["coara_name"] = coara_name
    if metadata:
        record["metadata"] = _truncate_value(metadata)
    if traceback:
        tb_text, tb_truncated = _compact_text(traceback, max_chars=_MAX_TRACEBACK_CHARS)
        record["traceback"] = tb_text
        if tb_truncated:
            record["traceback_truncated"] = True
    if msg_truncated:
        record["message_truncated"] = True

    log_path = resolve_error_log_path(workspace_dir, coara_home=coara_home)
    _append_record(log_path, record)
    return log_path


__all__ = [
    "classify_tool_error",
    "log_session_error_event",
    "log_tool_error_event",
    "purge_session_audit_logs",
    "resolve_error_log_path",
    "resolve_workspace_errors_log_path",
]

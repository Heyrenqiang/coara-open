"""Tests for workspace unified error log."""

from __future__ import annotations

import json
from pathlib import Path

from src.core import error_log as error_log_module
from src.core.coara_home import CoaraHomePaths
from src.core.error_log import (
    classify_tool_error,
    log_session_error_event,
    log_tool_error_event,
    purge_session_audit_logs,
    resolve_error_log_path,
    resolve_workspace_errors_log_path,
)
from src.core.error_log_query import resolve_error_log_paths, summarize_errors


def test_resolve_workspace_errors_log_path_uses_workspace_dot_coara(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert resolve_workspace_errors_log_path(workspace) == workspace / ".coara" / "logs" / "errors.jsonl"


def test_resolve_error_log_path_follows_coara_home(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    expected = CoaraHomePaths.for_workspace(workspace).logs_dir / "errors.jsonl"
    assert resolve_error_log_path(workspace) == expected


def test_log_tool_error_event_writes_structured_record(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = log_tool_error_event(
        workspace_dir=workspace,
        session_id="sess-1",
        coara_id="root",
        coara_name="Root",
        source_event="tool_complete",
        tool_name="shell",
        tool_call_id="tc-1",
        message="Sandbox denied: rm",
        arguments={"command": "rm -rf /"},
        duration_ms=12.5,
    )
    assert path == resolve_error_log_path(workspace)
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["kind"] == "tool_error"
    assert payload["error_kind"] == "sandbox"
    assert payload["tool"] == "shell"
    assert payload["arguments"]["command"] == "rm -rf /"


def test_log_session_error_event(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = log_session_error_event(
        workspace_dir=workspace,
        session_id="sess-4",
        coara_id="root",
        coara_name="Root",
        event="llm_error",
        message="Provider timeout",
        metadata={"model": "test-model"},
    )
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["kind"] == "session_error"
    assert payload["error_kind"] == "llm_error"
    assert payload["metadata"]["model"] == "test-model"


def test_error_log_rotates_archive_when_exceeding_limit(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(error_log_module, "_MAX_LOG_BYTES", 200)
    path = resolve_error_log_path(workspace)
    for _ in range(6):
        log_session_error_event(
            workspace_dir=workspace,
            session_id="sess-rotate",
            coara_id="root",
            event="llm_error",
            message="x" * 80,
        )
    archive = path.with_name(f"{path.name}.1")
    assert path.is_file()
    assert archive.is_file()


def test_resolve_error_log_paths_includes_archive_and_legacy(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    primary = resolve_error_log_path(workspace)
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("{}\n", encoding="utf-8")
    archive = primary.with_name(f"{primary.name}.1")
    archive.write_text("{}\n", encoding="utf-8")
    legacy = resolve_workspace_errors_log_path(workspace)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}\n", encoding="utf-8")

    paths = resolve_error_log_paths(workspace)

    assert primary in paths
    assert archive in paths
    assert legacy in paths


def test_summarize_errors_aggregates_and_filters(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    log_tool_error_event(
        workspace_dir=workspace,
        session_id="sess-1",
        coara_id="root",
        coara_name="Root",
        source_event="tool_complete",
        tool_name="shell",
        tool_call_id="tc-1",
        message="Sandbox denied: rm",
    )
    log_tool_error_event(
        workspace_dir=workspace,
        session_id="sess-1",
        coara_id="root",
        coara_name="Root",
        source_event="tool_complete",
        tool_name="read",
        tool_call_id="tc-2",
        message="read failed",
    )
    log_session_error_event(
        workspace_dir=workspace,
        session_id="sess-2",
        coara_id="root",
        event="llm_error",
        message="Provider timeout",
    )

    summary = summarize_errors(workspace_dir=workspace, days=0)

    assert summary.total == 3
    assert summary.kinds["sandbox"] == 1
    assert summary.kinds["llm_error"] == 1
    assert summary.tools["shell"] == 1
    assert summary.sessions["sess-1"] == 2

    filtered = summarize_errors(workspace_dir=workspace, days=0, tool="read")
    assert filtered.total == 1
    assert filtered.tools["read"] == 1


def test_purge_session_audit_logs_removes_legacy_dirs(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_home = tmp_path / "coara-home"
    paths = CoaraHomePaths.for_workspace(workspace, configured_home=global_home, migrate=False)
    legacy = paths.logs_dir / "sessions"
    legacy.mkdir(parents=True)
    (legacy / "old.jsonl").write_text("{}", encoding="utf-8")
    local_legacy = workspace / ".coara" / "logs" / "sessions"
    local_legacy.mkdir(parents=True)
    (local_legacy / "old.jsonl").write_text("{}", encoding="utf-8")

    purge_session_audit_logs(workspace_dir=workspace, coara_home=global_home)

    assert not legacy.exists()
    assert not local_legacy.exists()


def test_classify_tool_error_user_denied() -> None:
    assert classify_tool_error(event="tool_complete", message="Tool 'shell' was cancelled by user.") == "user_denied"

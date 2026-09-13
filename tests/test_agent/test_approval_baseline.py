"""P0-13: approval binds target file content; refuse if it drifts before execute."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agent.approval_baseline import (
    ApprovalFileDriftError,
    capture_approval_file_baseline,
    file_content_token,
    verify_approval_file_baseline,
)
from src.agent.tool_policy import ToolExecutionPolicy
from src.tools.builtin.file_io.edit import EditToolInvocation
from src.tools.builtin.file_io.write import WriteToolInvocation


def test_file_content_token_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("v1", encoding="utf-8")
    t1 = file_content_token(path)
    path.write_text("v2", encoding="utf-8")
    assert file_content_token(path) != t1


def test_verify_detects_drift_after_capture(tmp_path: Path) -> None:
    path = tmp_path / "target.txt"
    path.write_text("before", encoding="utf-8")
    inv = MagicMock()
    inv.path = str(path)
    inv.params = {"path": str(path)}

    assert capture_approval_file_baseline(inv) is not None
    assert verify_approval_file_baseline(inv) is None

    path.write_text("after", encoding="utf-8")
    msg = verify_approval_file_baseline(inv)
    assert msg is not None
    assert "审批等待期间已被修改" in msg


def test_verify_detects_delete_during_window(tmp_path: Path) -> None:
    path = tmp_path / "gone.txt"
    path.write_text("x", encoding="utf-8")
    inv = MagicMock()
    inv.path = str(path)
    inv.params = {"path": str(path)}
    capture_approval_file_baseline(inv)
    path.unlink()
    msg = verify_approval_file_baseline(inv)
    assert msg is not None
    assert "删除或移动" in msg


def test_no_baseline_when_file_missing(tmp_path: Path) -> None:
    inv = MagicMock()
    inv.path = str(tmp_path / "new.txt")
    inv.params = {"path": inv.path}
    assert capture_approval_file_baseline(inv) is None
    assert verify_approval_file_baseline(inv) is None


@pytest.mark.asyncio
async def test_confirm_raises_on_drift_after_user_agrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cfg.txt"
    path.write_text("original", encoding="utf-8")

    class _FakeRemote:
        async def confirm(self, question, options, *, timeout_seconds=300.0, signal=None):  # noqa: ARG002
            path.write_text("mutated-during-prompt", encoding="utf-8")
            return True

    monkeypatch.setattr("src.coara.turn_context.get_end_channel", lambda: None)
    monkeypatch.setattr("src.coara.turn_context.get_turn_channel", lambda: _FakeRemote())

    inv = MagicMock()
    inv.path = str(path)
    inv.params = {"path": str(path)}
    inv.get_description.return_value = f"写入 {path}"

    with pytest.raises(ApprovalFileDriftError, match="审批等待期间已被修改"):
        await ToolExecutionPolicy.confirm_tool_execution(inv, level="prompt")


@pytest.mark.asyncio
async def test_resolve_surfaces_drift_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "out.txt"
    path.write_text("a", encoding="utf-8")

    class _FakeRemote:
        async def confirm(self, question, options, *, timeout_seconds=300.0, signal=None):  # noqa: ARG002
            path.write_text("b", encoding="utf-8")
            return True

    monkeypatch.setattr("src.coara.turn_context.get_end_channel", lambda: None)
    monkeypatch.setattr("src.coara.turn_context.get_turn_channel", lambda: _FakeRemote())

    inv = MagicMock()
    inv.path = str(path)
    inv.params = {"path": str(path), "contents": "new"}
    inv.get_description.return_value = f"写入 {path}"

    class _AlwaysPrompt:
        @staticmethod
        def requires_approval(_args: dict) -> bool:
            return True

    config_manager = MagicMock()
    config_manager.get_security_config.return_value = {"call_policy": {}}
    coara = MagicMock()
    coara.identity = MagicMock(persona=MagicMock(name="root"))
    coara._origin_remote_channel = None

    decision = await ToolExecutionPolicy(config_manager).resolve(
        coara,
        "write",
        _AlwaysPrompt,
        {"path": str(path), "contents": "new"},
        inv,
        None,
    )
    assert decision.allowed is False
    assert "审批等待期间已被修改" in (decision.reason or "")


@pytest.mark.asyncio
async def test_edit_execute_refuses_when_baseline_drifted(tmp_path: Path) -> None:
    path = tmp_path / "edit_me.txt"
    path.write_text("hello world", encoding="utf-8")
    inv = EditToolInvocation(
        {"path": str(path), "old_string": "hello", "new_string": "hi"},
        read_state_store={},
        workspace_root=tmp_path,
    )
    capture_approval_file_baseline(inv)
    path.write_text("hello WORLD", encoding="utf-8")

    result = await inv.execute()
    assert result.is_error
    assert "审批等待期间已被修改" in str(result.content)
    assert path.read_text(encoding="utf-8") == "hello WORLD"


@pytest.mark.asyncio
async def test_edit_execute_ok_when_baseline_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "stable.txt"
    path.write_text("alpha beta", encoding="utf-8")
    inv = EditToolInvocation(
        {"path": str(path), "old_string": "alpha", "new_string": "ALPHA"},
        read_state_store={},
        workspace_root=tmp_path,
    )
    capture_approval_file_baseline(inv)
    result = await inv.execute()
    assert not result.is_error
    assert path.read_text(encoding="utf-8") == "ALPHA beta"


@pytest.mark.asyncio
async def test_write_execute_refuses_when_baseline_drifted(tmp_path: Path) -> None:
    path = tmp_path / "overwrite.txt"
    path.write_text("old", encoding="utf-8")
    inv = WriteToolInvocation(
        {"path": str(path), "contents": "new"},
        read_state_store={},
        workspace_root=tmp_path,
    )
    capture_approval_file_baseline(inv)
    path.write_text("external", encoding="utf-8")

    result = await inv.execute()
    assert result.is_error
    assert "审批等待期间已被修改" in str(result.content)
    assert path.read_text(encoding="utf-8") == "external"

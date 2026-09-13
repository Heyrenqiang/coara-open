"""#90: delete 回收站 — 工作区内文件移入 .coara/trash/，TTL 惰性清理"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.tools.builtin.file_io.delete import DeleteTool
from src.tools.builtin.file_io.trash import (
    TRASH_TTL_SECONDS,
    move_to_trash,
    prune_expired_trash,
    trash_root_for,
)


def _make_tool(workspace: Path) -> DeleteTool:
    return DeleteTool(read_state_store={}, workspace_root=workspace)


@pytest.mark.asyncio
async def test_delete_moves_workspace_file_to_trash(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    target = workspace / "src" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('hi')\n", encoding="utf-8")

    result = await _make_tool(workspace).create_invocation({"path": str(target)}).execute()

    assert not result.is_error
    assert not target.exists()
    assert "回收站" in result.content
    trash_entries = list((workspace / ".coara" / "trash" / "src").glob("main.py.*"))
    assert len(trash_entries) == 1
    assert trash_entries[0].read_text(encoding="utf-8") == "print('hi')\n"
    assert result.metadata["trash_path"] == str(trash_entries[0])


@pytest.mark.asyncio
async def test_delete_trash_same_name_no_collision(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    # 不同目录下的同名文件：相对路径结构保留，互不覆盖
    first = workspace / "a" / "config.yaml"
    second = workspace / "b" / "config.yaml"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    tool = _make_tool(workspace)

    assert not (await tool.create_invocation({"path": str(first)}).execute()).is_error
    assert not (await tool.create_invocation({"path": str(second)}).execute()).is_error

    assert len(list((workspace / ".coara" / "trash" / "a").glob("config.yaml.*"))) == 1
    assert len(list((workspace / ".coara" / "trash" / "b").glob("config.yaml.*"))) == 1

    # 同一路径同名文件删除两次：时间戳后缀区分，不互踩
    again = workspace / "a" / "config.yaml"
    again.write_text("three", encoding="utf-8")
    assert not (await tool.create_invocation({"path": str(again)}).execute()).is_error
    assert len(list((workspace / ".coara" / "trash" / "a").glob("config.yaml.*"))) == 2


@pytest.mark.asyncio
async def test_delete_outside_workspace_deletes_permanently(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere" / "scratch.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("bye", encoding="utf-8")

    result = await _make_tool(workspace).create_invocation({"path": str(outside)}).execute()

    assert not result.is_error
    assert not outside.exists()
    assert "永久删除" in result.content
    assert result.metadata.get("permanent") is True
    assert not (workspace / ".coara" / "trash").exists()


@pytest.mark.asyncio
async def test_delete_vault_open_file_bypasses_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    target = workspace / ".coara" / "vault" / "open" / "secret.txt"
    target.parent.mkdir(parents=True)
    target.write_text("plaintext secret", encoding="utf-8")
    # 宝箱 open/ 明文不进回收站（避免封存语义被副本削弱）
    monkeypatch.setattr("src.vault.guard.is_under_vault_open", lambda p: True)

    result = await _make_tool(workspace).create_invocation({"path": str(target)}).execute()

    assert not result.is_error
    assert not target.exists()
    assert "永久删除" in result.content
    assert not (workspace / ".coara" / "trash").exists()


def test_prune_expired_trash_removes_old_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    old_file = workspace / "old" / "stale.txt"
    new_file = workspace / "old" / "fresh.txt"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")

    old_trash = move_to_trash(old_file, workspace)
    new_trash = move_to_trash(new_file, workspace)
    # 把旧条目的入站时间拨到 TTL 之前
    expired_time = time.time() - TRASH_TTL_SECONDS - 60
    os.utime(old_trash, (expired_time, expired_time))

    removed = prune_expired_trash(workspace)

    assert removed == 1
    assert not old_trash.exists()
    assert new_trash.exists()
    # 目录未腾空（仍有 fresh 条目），回收站根保留
    assert trash_root_for(workspace).is_dir()


def test_prune_expired_trash_cleans_empty_dirs(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    target = workspace / "deep" / "nested" / "gone.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    trashed = move_to_trash(target, workspace)
    expired_time = time.time() - TRASH_TTL_SECONDS - 60
    os.utime(trashed, (expired_time, expired_time))

    removed = prune_expired_trash(workspace)

    assert removed == 1
    assert trash_root_for(workspace).is_dir()
    assert not (trash_root_for(workspace) / "deep").exists()

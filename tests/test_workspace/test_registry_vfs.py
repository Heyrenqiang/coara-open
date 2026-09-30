"""Tests for workspace registry and VFS resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace.registry import WorkspaceRegistry
from src.workspace.types import MountMode, WorkspaceKind
from src.workspace.vfs import VfsResolutionError, VfsResolver


@pytest.fixture
def registry(tmp_path: Path) -> WorkspaceRegistry:
    home = tmp_path / "coara-home"
    reg = WorkspaceRegistry(home)
    reg.load()
    return reg


def test_registry_ensure_workspace(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    workspace = tmp_path / "my-app"
    workspace.mkdir()
    entry = registry.ensure_workspace(workspace, name="my-app")
    assert entry.name == "my-app"
    assert entry.kind == WorkspaceKind.NORMAL
    assert entry.resolved_path() == workspace.resolve()
    again = registry.ensure_workspace(workspace)
    assert again.name == "my-app"


def test_registry_ensure_workspace_summary(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    workspace = tmp_path / "xuan"
    workspace.mkdir()
    entry = registry.ensure_workspace(workspace, name="xuan", summary="Android Matrix client")
    assert entry.summary == "Android Matrix client"
    again = registry.ensure_workspace(workspace, summary="ignored when already set")
    assert again.summary == "Android Matrix client"


def test_registry_rename_keeps_id_and_path(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    workspace = tmp_path / "android-app"
    workspace.mkdir()
    entry = registry.ensure_workspace(workspace, name="android-app")
    old_id = entry.id
    old_path = entry.path

    renamed = registry.rename(entry.id, "coara-app-实验版")
    assert renamed is not None
    assert renamed.id == old_id
    assert renamed.path == old_path
    assert renamed.name == "coara-app-实验版"
    assert registry.resolve_name_or_id("android-app") is None
    assert registry.resolve_name_or_id("coara-app-实验版") is not None

    with pytest.raises(ValueError, match="不能为空"):
        registry.rename(renamed.id, "  ")
    other = tmp_path / "other"
    other.mkdir()
    registry.ensure_workspace(other, name="taken")
    with pytest.raises(ValueError, match="已存在"):
        registry.rename(renamed.id, "taken")


def test_vfs_resolve_absolute_and_relative(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sub = workspace / "src"
    sub.mkdir()
    entry = registry.ensure_workspace(workspace, name="ws")

    vfs = VfsResolver()
    vfs.set_mounts([entry], active_id=entry.id)

    resolved = vfs.resolve(str(sub.resolve()))
    assert resolved.workspace_id == entry.id
    assert resolved.path == sub.resolve()

    with pytest.raises(VfsResolutionError, match="绝对路径"):
        vfs.resolve("src")


def test_vfs_rejects_outside_mount(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    entry = registry.ensure_workspace(workspace, name="ws")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    vfs = VfsResolver()
    vfs.set_mounts([entry], active_id=entry.id)

    from src.workspace.vfs import UnmountedPathError

    with pytest.raises(UnmountedPathError, match="不在任何已挂载工作空间内"):
        vfs.resolve(str(outside))


def test_resolve_workspace_path_outside_mount_falls_through_for_writes(tmp_path: Path) -> None:
    """Non-strict resolver: out-of-mount writes are NOT denied at the path layer.

    The approval gate (tool-declared requires_approval) handles them upstream.
    """
    from src.tools.builtin.file_io.file_support import (
        path_outside_workspace_mounts,
        resolve_workspace_path,
    )
    from src.workspace.types import WorkspaceEntry, WorkspaceKind

    workspace = tmp_path / "ws"
    workspace.mkdir()
    entry = WorkspaceEntry(
        id="ws-id",
        name="ws",
        path=str(workspace),
        kind=WorkspaceKind.NORMAL,
    )
    vfs = VfsResolver()
    vfs.set_mounts([entry], active_id=entry.id)

    outside = tmp_path / "outside.txt"
    path, err = resolve_workspace_path(str(outside), workspace, "Write", vfs_resolver=vfs)
    assert err is None
    assert path == outside.resolve()
    assert path_outside_workspace_mounts(vfs, workspace, str(outside)) is True
    assert path_outside_workspace_mounts(vfs, workspace, str(workspace / "in.txt")) is False
    # 相对路径 / 空路径不触发越界审批判定
    assert path_outside_workspace_mounts(vfs, workspace, "rel.txt") is False
    assert path_outside_workspace_mounts(vfs, workspace, "") is False


def test_resolve_workspace_path_strict_resolver_still_denies(tmp_path: Path) -> None:
    """Delegate-scoped (strict) resolvers keep the hard deny for out-of-mount paths."""
    from src.tools.builtin.file_io.file_support import resolve_workspace_path
    from src.workspace.types import WorkspaceEntry, WorkspaceKind

    workspace = tmp_path / "ws"
    workspace.mkdir()
    entry = WorkspaceEntry(
        id="ws-id",
        name="ws",
        path=str(workspace),
        kind=WorkspaceKind.NORMAL,
    )
    vfs = VfsResolver(strict=True)
    vfs.set_mounts([entry], active_id=entry.id)

    path, err = resolve_workspace_path(str(tmp_path / "outside.txt"), workspace, "Edit", vfs_resolver=vfs)
    assert path is None
    assert err is not None and "不在任何已挂载工作空间内" in err


def test_vfs_resolve_in_workspace_by_name(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    workspace = tmp_path / "xuan_android_local"
    workspace.mkdir()
    inbox = workspace / "feedback" / "inbox"
    inbox.mkdir(parents=True)
    entry = registry.ensure_workspace(workspace, name="暄")

    vfs = VfsResolver()
    vfs.set_mounts([entry], active_id=entry.id)

    resolved = vfs.resolve_in_workspace("暄", "feedback/inbox", action="watch")
    assert resolved.path == inbox.resolve()
    assert resolved.workspace_id == entry.id

    with pytest.raises(VfsResolutionError, match="未挂载"):
        vfs.resolve_in_workspace("missing", "feedback/inbox")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    entry = registry.ensure_workspace(workspace, name="ws")
    entry.mode = MountMode.READ_ONLY

    vfs = VfsResolver()
    vfs.set_mounts([entry], active_id=entry.id)
    summary = vfs.format_mount_list()
    assert "read-only" in summary


def test_resolve_workspace_path_rejects_write_on_read_only_mount(tmp_path: Path) -> None:
    """只读引用（挂载 mode=ro）的写类工具在路径解析处硬拒；只读工具与 rw 空间不受影响。"""
    from src.tools.builtin.file_io.file_support import resolve_workspace_path
    from src.workspace.types import WorkspaceEntry, WorkspaceKind

    workspace = tmp_path / "ws-ro"
    workspace.mkdir()
    entry = WorkspaceEntry(
        id="ws-ro-id",
        name="ro",
        path=str(workspace),
        kind=WorkspaceKind.NORMAL,
        mode=MountMode.READ_ONLY,
    )
    vfs = VfsResolver()
    vfs.set_mounts([entry], active_id=entry.id)

    target = workspace / "a.txt"
    blocked, err = resolve_workspace_path(str(target), workspace, "Write", vfs_resolver=vfs)
    assert blocked is None
    assert err is not None and "只读" in err

    # read/grep/glob（read_only=True）照常解析
    allowed, read_err = resolve_workspace_path(str(target), workspace, "Read", vfs_resolver=vfs, read_only=True)
    assert read_err is None and allowed == target.resolve()

    # mode=rw 的普通空间不受影响
    entry.mode = MountMode.READ_WRITE
    ok, ok_err = resolve_workspace_path(str(target), workspace, "Write", vfs_resolver=vfs)
    assert ok_err is None and ok == target.resolve()


def test_load_migrates_legacy_kinds(tmp_path: Path, monkeypatch) -> None:
    """磁盘旧 kind（managed/reference/code）加载时改写为 normal 并落盘。"""
    # 跳过 ephemeral 裁剪，避免 tmp_path 被当成 pytest 临时空间删掉
    monkeypatch.setattr(
        "src.workspace.registry.is_ephemeral_workspace_path",
        lambda _p: False,
    )
    home = tmp_path / "coara-home"
    registry_dir = home / "registry"
    registry_dir.mkdir(parents=True)
    ws = tmp_path / "legacy-ws"
    ws.mkdir()
    from src.core.coara_home import workspace_id_for

    wid = workspace_id_for(ws)
    yaml_text = f"""version: 1
default_workspace: {wid}
workspaces:
  {wid}:
    name: legacy
    id: {wid}
    path: {ws.resolve().as_posix()}
    kind: managed
    status: active
    mode: rw
"""
    (registry_dir / "workspaces.yaml").write_text(yaml_text, encoding="utf-8")
    reg = WorkspaceRegistry(home)
    doc = reg.load()
    entry = doc.workspaces[wid]
    assert entry.kind == WorkspaceKind.NORMAL
    # 再读磁盘确认已持久化
    reloaded = WorkspaceRegistry(home)
    reloaded.load()
    assert reloaded.document.workspaces[wid].kind == WorkspaceKind.NORMAL

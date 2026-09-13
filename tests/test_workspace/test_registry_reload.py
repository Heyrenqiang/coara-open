"""#24：registry 热重载后 active 被外部删除时挂载快照必须失效。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace.manager import WorkspaceManager
from src.workspace.registry import WorkspaceRegistry
from src.workspace.vfs import UnmountedPathError


@pytest.fixture
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    # registry.py 按名导入该函数；在其命名空间打补丁，tmp_path 空间才会持久化
    monkeypatch.setattr(
        "src.workspace.registry.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    ws_a = tmp_path / "ws-a"
    ws_a.mkdir()
    return WorkspaceManager(ws_a, coara_home=tmp_path / "home")


@pytest.mark.asyncio
async def test_reload_registry_clears_mounts_when_active_removed(manager: WorkspaceManager, tmp_path: Path) -> None:
    await manager.initialize()
    ws_b = tmp_path / "ws-b"
    ws_b.mkdir()
    manager.add_workspace(ws_b, name="ws-b")
    assert manager.switch("ws-b")
    active = manager.vfs.active_entry
    assert active is not None and active.name == "ws-b"

    # 模拟外部进程改写 registry：删除当前 active 空间
    external = WorkspaceRegistry(manager.coara_home)
    external.load()
    entry = external.resolve_name_or_id("ws-b")
    assert entry is not None
    assert external.remove(entry.id)

    manager.reload_registry()

    # 挂载快照已清空：已注销空间不再可解析/可写
    assert manager.vfs.active_entry is None
    with pytest.raises(UnmountedPathError):
        manager.vfs.resolve(str((ws_b / "x.txt").resolve()))


@pytest.mark.asyncio
async def test_reload_registry_reapplies_mounts_when_active_kept(manager: WorkspaceManager) -> None:
    await manager.initialize()
    active_id = manager.vfs.active_id

    manager.reload_registry()

    # active 仍在：挂载重建且 active 保持
    assert manager.vfs.active_id == active_id
    assert manager.vfs.active_entry is not None

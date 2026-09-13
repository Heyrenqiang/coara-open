"""#92: reveal_tool 揭示状态持久化 — 重启/恢复后可还原，失效工具不复活"""

from __future__ import annotations

from pathlib import Path

from src.coara.tool_manager import ToolManager
from src.coara.workspace_state import _revealed_tools_path, save_revealed_tools


class _FakeTool:
    """最小工具桩：只携带 ToolManager 注册/可见性路径读取的属性"""

    def __init__(self, name: str, *, should_defer: bool = False, workspace_root: Path | None = None):
        self.name = name
        self.should_defer = should_defer
        self.owner_only = False
        self.category = "system" if should_defer else "filesystem"
        self.description = f"fake tool {name}"
        if workspace_root is not None:
            self._workspace_root = workspace_root

    @property
    def definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": {}},
        }


def _llm_visible_names(manager: ToolManager) -> set[str]:
    return {td["name"] for td in manager.get_tool_definitions_for_llm(False)}


def test_reveal_persists_and_restores_for_new_manager(tmp_path: Path) -> None:
    first = ToolManager()
    first.register_tool(_FakeTool("probe", workspace_root=tmp_path))
    first.register_tool(_FakeTool("snap_tool", should_defer=True))
    assert first.reveal_tool("snap_tool")
    assert _revealed_tools_path(tmp_path).exists()

    # 模拟重启：全新 ToolManager 重新注册同一批工具
    second = ToolManager()
    second.register_tool(_FakeTool("probe", workspace_root=tmp_path))
    second.register_tool(_FakeTool("snap_tool", should_defer=True))

    assert "snap_tool" in second._revealed
    assert "snap_tool" in _llm_visible_names(second)


def test_restore_does_not_revive_unregistered_tool(tmp_path: Path) -> None:
    save_revealed_tools(tmp_path, ["ghost_tool"])

    manager = ToolManager()
    manager.register_tool(_FakeTool("probe", workspace_root=tmp_path))
    manager.register_tool(_FakeTool("real_tool", should_defer=True))

    assert "ghost_tool" not in manager._revealed
    assert "ghost_tool" not in _llm_visible_names(manager)
    # 未注册的恢复条目不影响在册挂起工具的隐藏语义
    assert "real_tool" not in _llm_visible_names(manager)


def test_restore_applies_when_tool_registers_later(tmp_path: Path) -> None:
    # 工具可能恢复时尚未注册，注册到达时才生效
    save_revealed_tools(tmp_path, ["late_tool"])

    manager = ToolManager()
    manager.register_tool(_FakeTool("probe", workspace_root=tmp_path))
    assert "late_tool" not in manager._revealed

    manager.register_tool(_FakeTool("late_tool", should_defer=True))

    assert "late_tool" in manager._revealed
    assert "late_tool" in _llm_visible_names(manager)


def test_restore_drops_tool_no_longer_deferred(tmp_path: Path) -> None:
    save_revealed_tools(tmp_path, ["now_eager"])

    manager = ToolManager()
    manager.register_tool(_FakeTool("probe", workspace_root=tmp_path))
    manager.register_tool(_FakeTool("now_eager", should_defer=False))

    # 工具已非延迟（天然可见），恢复的揭示状态直接丢弃
    assert "now_eager" not in manager._revealed
    assert "now_eager" in _llm_visible_names(manager)


def test_clear_revealed_clears_persistence(tmp_path: Path) -> None:
    first = ToolManager()
    first.register_tool(_FakeTool("probe", workspace_root=tmp_path))
    first.register_tool(_FakeTool("snap_tool", should_defer=True))
    assert first.reveal_tool("snap_tool")

    first.clear_revealed()

    second = ToolManager()
    second.register_tool(_FakeTool("probe", workspace_root=tmp_path))
    second.register_tool(_FakeTool("snap_tool", should_defer=True))
    assert "snap_tool" not in second._revealed
    assert "snap_tool" not in _llm_visible_names(second)

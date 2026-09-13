"""/tools on|off 运行时工具开关：三端共享命令层，禁用即时生效并写回 config.yaml。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.coara.commands.registry import execute_command
from src.core.types import ToolCall
from tests.helpers import make_test_coara


@pytest.mark.asyncio
async def test_tools_off_applies_disabled_and_persists(monkeypatch) -> None:
    applied: list[list[str]] = []

    root = SimpleNamespace(
        apply_tools_disabled=lambda names: applied.append(list(names)),
        get_status=lambda: {"tool_visibility": {"visible": ["read"], "hidden": {}}},
    )

    from src.core import config as config_mod

    fake_cfg = SimpleNamespace(tools=SimpleNamespace(disabled=[]))
    monkeypatch.setattr(config_mod.config_manager, "_config", fake_cfg)
    persisted: list[list[str]] = []
    monkeypatch.setattr(config_mod.config_manager, "set_tools_disabled", lambda names: persisted.append(list(names)))

    result = await execute_command(root, "/tools off edit")
    assert "已停用 edit" in result.output
    assert "运行时立即生效" in result.output
    assert applied == [["edit"]]
    assert persisted == [["edit"]]


@pytest.mark.asyncio
async def test_tools_on_removes_from_disabled(monkeypatch) -> None:
    applied: list[list[str]] = []

    root = SimpleNamespace(
        apply_tools_disabled=lambda names: applied.append(list(names)),
        get_status=lambda: {"tool_visibility": {"visible": ["read"], "hidden": {}}},
    )

    from src.core import config as config_mod

    fake_cfg = SimpleNamespace(tools=SimpleNamespace(disabled=["edit"]))
    monkeypatch.setattr(config_mod.config_manager, "_config", fake_cfg)
    persisted: list[list[str]] = []
    monkeypatch.setattr(config_mod.config_manager, "set_tools_disabled", lambda names: persisted.append(list(names)))

    result = await execute_command(root, "/tools on edit")
    assert "已启用 edit" in result.output
    assert applied == [[]]
    assert persisted == [[]]


@pytest.mark.asyncio
async def test_tools_on_keeps_other_disabled(monkeypatch) -> None:
    applied: list[list[str]] = []

    root = SimpleNamespace(
        apply_tools_disabled=lambda names: applied.append(list(names)),
        get_status=lambda: {"tool_visibility": {"visible": ["read"], "hidden": {}}},
    )

    from src.core import config as config_mod

    fake_cfg = SimpleNamespace(tools=SimpleNamespace(disabled=["edit", "delete"]))
    monkeypatch.setattr(config_mod.config_manager, "_config", fake_cfg)
    persisted: list[list[str]] = []
    monkeypatch.setattr(config_mod.config_manager, "set_tools_disabled", lambda names: persisted.append(list(names)))

    result = await execute_command(root, "/tools on edit")
    assert "已启用 edit" in result.output
    assert applied == [["delete"]]
    assert persisted == [["delete"]]


@pytest.mark.asyncio
async def test_tools_off_missing_name_returns_error() -> None:
    root = SimpleNamespace(
        apply_tools_disabled=lambda names: None,
        get_status=lambda: {"tool_visibility": {"visible": [], "hidden": {}}},
    )
    result = await execute_command(root, "/tools off")
    assert result.data.get("error") is True


def test_set_tools_disabled_persists_to_config_yaml(tmp_path: Path, monkeypatch) -> None:
    import yaml

    from src.core import config as config_mod

    target = tmp_path / "config.yaml"
    target.write_text("tools:\n  disabled: []\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "_find_writable_config_yaml", lambda raw: target)
    monkeypatch.setattr(
        config_mod.config_manager,
        "_config",
        SimpleNamespace(tools=SimpleNamespace(disabled=[])),
    )

    config_mod.config_manager.set_tools_disabled(["edit", "delete"])

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["tools"]["disabled"] == ["edit", "delete"]
    assert config_mod.config_manager.config.tools.disabled == ["edit", "delete"]


@pytest.mark.asyncio
async def test_executor_rejects_disabled_tool(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path)
    await coara.initialize()
    coara._tool_manager.set_disabled({"glob"})

    executions = await coara.tool_executor.execute(
        coara,
        [ToolCall(id="call-1", name="glob", arguments={"pattern": "*.py"})],
        is_owner=True,
    )
    result = executions[0].result
    assert result.is_error
    content = str(result.content)
    assert "已被禁用" in content
    assert "glob" in content
    assert "/tools on" in content


@pytest.mark.asyncio
async def test_system_prompt_mentions_disabled_tools(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path)
    await coara.initialize()
    coara._tool_manager.set_disabled({"edit"})

    prompt = coara._build_system_prompt()
    assert "已被禁用" in prompt
    assert "edit" in prompt
    assert "/tools on" in prompt

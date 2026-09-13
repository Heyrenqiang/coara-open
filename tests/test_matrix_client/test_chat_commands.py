"""Matrix slash commands."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.matrix_client.chat_commands import (
    _matrix_visible_output,
    is_stop_command,
    try_handle_matrix_chat_command,
)


def test_matrix_ws_switch_hides_workspace_path() -> None:
    from src.coara.commands.types import CommandResult

    result = CommandResult(
        output="已切换到工作空间 shop 上次对话 14:30\nD:\\code_ws\\shop\n已设为默认工作空间",
        action="switch_workspace",
        data={"name": "shop", "workspace_dir": "D:\\code_ws\\shop", "default_set": True},
    )
    visible = _matrix_visible_output(result)
    assert "已切换到工作空间 shop 上次对话 14:30" in visible
    assert "已设为默认工作空间" in visible
    assert "D:\\code_ws\\shop" not in visible


def test_is_stop_command_normalizes_source_tag() -> None:
    assert is_stop_command("/stop")
    assert is_stop_command("  /STOP  ")


@pytest.mark.asyncio
async def test_try_handle_stop_interrupts_active_turn() -> None:
    foreground = SimpleNamespace(
        interrupt_current_turn=MagicMock(return_value=True),
    )
    root = SimpleNamespace(foreground_coara=foreground)
    sent: list[str] = []

    async def send_text(body: str) -> None:
        sent.append(body)

    handled = await try_handle_matrix_chat_command(root, "/stop", send_text=send_text)

    assert handled is True
    foreground.interrupt_current_turn.assert_called_once_with("user_stop", interrupt_source="stop_command")
    assert sent == ["已中断当前回合"]


@pytest.mark.asyncio
async def test_try_handle_stop_when_idle() -> None:
    foreground = SimpleNamespace(
        interrupt_current_turn=MagicMock(return_value=False),
    )
    root = SimpleNamespace(foreground_coara=foreground)
    sent: list[str] = []

    async def send_text(body: str) -> None:
        sent.append(body)

    handled = await try_handle_matrix_chat_command(root, "/stop", send_text=send_text)

    assert handled is True
    assert sent == ["当前没有运行中的回合"]

"""interactive_prompt 单元测试：prompt_password 超时路径。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.cli.interactive_prompt import prompt_password


class _FakeSpinner:
    def __init__(self) -> None:
        self.attached: list[Any] = []
        self.detached: list[Any] = []

    def attach_modal(self, delegate: Any) -> None:
        self.attached.append(delegate)

    def detach_modal(self, delegate: Any) -> None:
        self.detached.append(delegate)


@pytest.mark.asyncio
async def test_prompt_password_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2-4：prompt_password 加 timeout=300，超时自动 detach 并回显「已超时」。"""
    spinner = _FakeSpinner()
    monkeypatch.setattr("src.cli.interactive_prompt._modal_spinner", spinner)
    # 超时调极短（测试不等 300s）
    result = await prompt_password("保险柜解锁", timeout=0.01)
    assert result is None
    # modal 已 detach
    assert len(spinner.attached) == 1
    assert len(spinner.detached) == 1
    assert spinner.attached[0] is spinner.detached[0]


@pytest.mark.asyncio
async def test_prompt_password_success_no_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2-4：正常输入路径（用户及时按 Enter）不受 timeout 影响。"""
    spinner = _FakeSpinner()
    monkeypatch.setattr("src.cli.interactive_prompt._modal_spinner", spinner)

    async def simulate_user_input() -> None:
        await asyncio.sleep(0)
        delegate = spinner.attached[0]
        # 模拟用户输入并确认
        delegate._future.set_result("mypassword")

    task = asyncio.create_task(simulate_user_input())
    result = await prompt_password("保险柜解锁", timeout=5.0)
    await task
    assert result == "mypassword"


@pytest.mark.asyncio
async def test_prompt_password_no_spinner_raises() -> None:
    """无 modal spinner 注册时仍抛 RuntimeError（契约不变）。"""
    import src.cli.interactive_prompt as mod

    old = mod._modal_spinner
    mod._modal_spinner = None
    try:
        with pytest.raises(RuntimeError, match="Modal spinner not registered"):
            await prompt_password("保险柜解锁")
    finally:
        mod._modal_spinner = old

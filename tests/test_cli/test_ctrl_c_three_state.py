"""Ctrl+C 三态语义的回归测试（attach 架构）。

最早 CLI 设计的三态：
- 输入区有内容 → 清空输入（键绑定层消费，不在这里测——见 test_session 键绑定测试）
- 回合运行中（spinner 在转）→ on_interrupt 打断在跑回合，prompt loop 继续
- 空闲 → on_interrupt 返回 False，prompt loop break → _PromptExit → 退出 CLI

attach 化后的缺陷：on_interrupt（attached_chat_runner._interrupt_current_turn_nowait）
无条件 return True，空闲 Ctrl+C 也被当成「已打断」，prompt loop 永不退出。
这里直接测 _prompt_loop 对两种 on_interrupt 返回值的分叉。
"""

from __future__ import annotations

import asyncio

import pytest

from src.cli.session import _prompt_loop, _PromptCtrlC, _PromptExit


class _FakePromptSession:
    """prompt_async 第一次抛 KeyboardInterrupt 的替身（Ctrl+C 键）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def prompt_async(self) -> str:
        self.calls += 1
        raise KeyboardInterrupt


@pytest.fixture(autouse=True)
def _no_real_stdout_patch(monkeypatch: pytest.MonkeyPatch):
    """_prompt_loop 内部 import 真实 patch_stdout（无控制台环境炸）；换成空上下文。"""
    import contextlib

    import prompt_toolkit.patch_stdout

    monkeypatch.setattr(
        prompt_toolkit.patch_stdout,
        "patch_stdout",
        lambda *a, **k: contextlib.nullcontext(),
    )


@pytest.mark.asyncio
async def test_ctrl_c_idle_exits_prompt_loop() -> None:
    """空闲（on_interrupt 返回 False）：prompt loop break，队列收到 _PromptExit。"""
    queue: asyncio.Queue = asyncio.Queue()
    session = _FakePromptSession()
    await _prompt_loop(session, queue, on_interrupt=lambda: False)
    assert queue.get_nowait().__class__ is _PromptExit
    assert session.calls == 1  # 没有继续循环


@pytest.mark.asyncio
async def test_ctrl_c_running_interrupts_and_continues() -> None:
    """回合运行中（on_interrupt 返回 True）：入队 _PromptCtrlC 并继续循环；
    第二次 Ctrl+C 空闲（返回 False）时正常退出。"""
    queue: asyncio.Queue = asyncio.Queue()
    session = _FakePromptSession()
    states = iter([True, False])  # 第一次运行中、第二次空闲
    await _prompt_loop(session, queue, on_interrupt=lambda: next(states))
    assert queue.get_nowait().__class__ is _PromptCtrlC
    assert queue.get_nowait().__class__ is _PromptExit
    assert session.calls == 2


@pytest.mark.asyncio
async def test_ctrl_c_on_interrupt_exception_treated_as_not_interrupted() -> None:
    """on_interrupt 自身抛错（取不到事件循环等）：按未打断处理，正常退出。"""
    queue: asyncio.Queue = asyncio.Queue()

    def _bad() -> bool:
        raise RuntimeError("no running loop")

    await _prompt_loop(_FakePromptSession(), queue, on_interrupt=_bad)
    assert queue.get_nowait().__class__ is _PromptExit

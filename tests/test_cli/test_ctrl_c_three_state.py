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
    """prompt_async 按序吐出 *inputs*，吐完抛 *end_with*（缺省 Ctrl+C 键）的替身。"""

    def __init__(self, inputs: list[str] | None = None, *, end_with: type[BaseException] = KeyboardInterrupt) -> None:
        self.calls = 0
        self._inputs = list(inputs or [])
        self._end_with = end_with

    async def prompt_async(self) -> str:
        self.calls += 1
        if self._inputs:
            return self._inputs.pop(0)
        raise self._end_with


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


@pytest.mark.asyncio
async def test_new_uses_dedicated_callback_not_interrupt_all() -> None:
    """`/new` 走 on_new_session（只停在飞回合），不碰 on_interrupt（含后台任务）。

    旧实现把 `/new` 接到 on_interrupt：其判据是 has_running_work，于是换个会话
    顺手把本空间后台任务全杀了。
    """
    queue: asyncio.Queue = asyncio.Queue()
    session = _FakePromptSession(inputs=["/new"], end_with=EOFError)
    calls = {"interrupt": 0, "new": 0}

    def _on_interrupt() -> bool:
        calls["interrupt"] += 1
        return False

    def _on_new_session() -> bool:
        calls["new"] += 1
        return True

    await _prompt_loop(session, queue, on_interrupt=_on_interrupt, on_new_session=_on_new_session)

    assert queue.get_nowait() == "/new"
    assert queue.get_nowait().__class__ is _PromptExit
    assert calls == {"interrupt": 0, "new": 1}


@pytest.mark.asyncio
async def test_new_without_dedicated_callback_does_not_interrupt() -> None:
    """未接 on_new_session（旧调用方）：`/new` 不再借道 on_interrupt 全局打断。"""
    queue: asyncio.Queue = asyncio.Queue()
    calls = {"interrupt": 0}

    def _on_interrupt() -> bool:
        calls["interrupt"] += 1
        return False

    await _prompt_loop(_FakePromptSession(inputs=["/new"], end_with=EOFError), queue, on_interrupt=_on_interrupt)

    assert queue.get_nowait() == "/new"
    assert queue.get_nowait().__class__ is _PromptExit
    assert calls["interrupt"] == 0


class _SigintForeground:
    """foreground 替身：记录打断调用（SIGINT 口径用）。"""

    def __init__(self, *, active_turn: bool, interrupt_returns: bool = True) -> None:
        self._active_turn = active_turn
        self._interrupt_returns = interrupt_returns
        self.interrupts: list[tuple[str, str]] = []

    def has_active_turn(self) -> bool:
        return self._active_turn

    def interrupt_current_turn(self, reason: str, interrupt_source: str = "") -> bool:
        self.interrupts.append((reason, interrupt_source))
        return self._interrupt_returns


class _SigintRoot:
    """root 替身：has_running_work 覆盖回合 + 后台任务（RootShim 口径，见其同名实现）。"""

    def __init__(self, *, active_turn: bool, background_work: bool, interrupt_returns: bool = True) -> None:
        self.foreground_coara = _SigintForeground(active_turn=active_turn, interrupt_returns=interrupt_returns)
        self._background_work = background_work

    def has_running_work(self) -> bool:
        return self.foreground_coara.has_active_turn() or self._background_work


def test_sigint_interrupts_when_only_background_work_runs() -> None:
    """SIGINT 与键盘 Ctrl+C 对齐：只有后台任务在跑也算「有活」→ 打断，不退出 CLI。"""
    from src.cli.session import ChatTurnInterruptState, _resolve_running_sigint_action

    root = _SigintRoot(active_turn=False, background_work=True)
    state = ChatTurnInterruptState()

    assert _resolve_running_sigint_action(root, state) == "interrupt"
    assert root.foreground_coara.interrupts == [("user_ctrl_c", "cli_sigint_turn")]
    assert state.interrupt_requested is True


def test_sigint_propagates_when_fully_idle() -> None:
    """全空闲：SIGINT 照旧冒泡（外层退出 CLI）。"""
    from src.cli.session import ChatTurnInterruptState, _resolve_running_sigint_action

    root = _SigintRoot(active_turn=False, background_work=False)

    assert _resolve_running_sigint_action(root, ChatTurnInterruptState()) == "propagate"
    assert root.foreground_coara.interrupts == []


def test_session_sigint_handler_interrupts_background_only_work() -> None:
    """无回合上下文（state=None）时同一把尺：后台在跑 → 处理掉 SIGINT，不冒泡退出。"""
    from src.cli.session import _handle_cli_sigint

    root = _SigintRoot(active_turn=False, background_work=True)
    handled = _handle_cli_sigint(root, None, None, signum=2, frame=None)

    assert handled is True
    assert root.foreground_coara.interrupts == [("user_ctrl_c", "cli_sigint_session")]

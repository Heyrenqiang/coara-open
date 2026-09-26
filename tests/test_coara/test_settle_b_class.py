"""B 类写动作执行前夺权 / 整会话打断（settle_b_class_before_handler）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coara.commands.registry import CommandArgs, settle_b_class_before_handler


def _args(name: str, raw: str, target: object, origin: str = "cli-attached") -> CommandArgs:
    parts = raw.split()
    sub = parts[1].lower() if len(parts) >= 2 else None
    value = parts[2] if len(parts) >= 3 else None
    args = CommandArgs(name=name, parts=parts, raw=raw, sub=sub, value=value)
    args.target_coara = target
    args.origin_source = origin
    return args


@pytest.mark.asyncio
async def test_idle_model_seizes_ownership() -> None:
    coara = SimpleNamespace(
        has_active_turn=lambda: False,
        take_session_ownership=MagicMock(),
        interrupt_current_turn=MagicMock(return_value=False),
    )
    await settle_b_class_before_handler(_args("model", "/model deepseek-flash", coara), SimpleNamespace())
    coara.take_session_ownership.assert_called_once_with("cli-attached")
    coara.interrupt_current_turn.assert_not_called()


@pytest.mark.asyncio
async def test_busy_model_interrupts_with_ownership_and_bg_cancel() -> None:
    coara = SimpleNamespace(
        has_active_turn=lambda: True,
        take_session_ownership=MagicMock(),
        interrupt_current_turn=MagicMock(return_value=True),
        _wait_for_process_lock_release=MagicMock(),
    )
    await settle_b_class_before_handler(_args("model", "/model deepseek-flash", coara), SimpleNamespace())
    coara.interrupt_current_turn.assert_called_once()
    kwargs = coara.interrupt_current_turn.call_args.kwargs
    assert kwargs["cancel_delegates"] is True
    assert kwargs["take_ownership_source"] == "cli-attached"
    coara.take_session_ownership.assert_not_called()


@pytest.mark.asyncio
async def test_busy_new_interrupts_without_killing_background() -> None:
    coara = SimpleNamespace(
        has_active_turn=lambda: True,
        interrupt_current_turn=MagicMock(return_value=True),
    )
    await settle_b_class_before_handler(_args("new", "/new", coara), SimpleNamespace())
    kwargs = coara.interrupt_current_turn.call_args.kwargs
    assert kwargs["cancel_delegates"] is False
    assert kwargs["take_ownership_source"] == "cli-attached"


@pytest.mark.asyncio
async def test_busy_compact_waits_for_lock_and_idle() -> None:
    wait = MagicMock()
    calls = {"n": 0}

    async def _wait() -> None:
        wait()

    def _has_turn() -> bool:
        calls["n"] += 1
        return calls["n"] < 3

    coara = SimpleNamespace(
        has_active_turn=_has_turn,
        interrupt_current_turn=MagicMock(return_value=True),
        _wait_for_process_lock_release=_wait,
    )
    await settle_b_class_before_handler(_args("compact", "/compact", coara), SimpleNamespace())
    wait.assert_called_once()
    assert calls["n"] >= 3


@pytest.mark.asyncio
async def test_stop_skipped() -> None:
    coara = SimpleNamespace(
        has_active_turn=lambda: True,
        interrupt_current_turn=MagicMock(),
        take_session_ownership=MagicMock(),
    )
    await settle_b_class_before_handler(_args("stop", "/stop", coara), SimpleNamespace())
    coara.interrupt_current_turn.assert_not_called()
    coara.take_session_ownership.assert_not_called()

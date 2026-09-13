"""Tests for AbortSignal wait helpers and Matrix approval abort wiring."""

from __future__ import annotations

import asyncio

import pytest

from src.core.abort import AbortController, OperationAborted, abortable_lock, wait_for_abortable


@pytest.mark.asyncio
async def test_wait_for_abortable_raises_when_signal_fires() -> None:
    controller = AbortController()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    async def _abort_soon() -> None:
        await asyncio.sleep(0.01)
        controller.abort("user_ctrl_c")

    abort_task = asyncio.create_task(_abort_soon())
    with pytest.raises(OperationAborted) as caught:
        await wait_for_abortable(future, controller.signal, timeout=5.0)
    await abort_task
    assert caught.value.reason == "user_ctrl_c"
    assert not future.done() or future.cancelled()


@pytest.mark.asyncio
async def test_wait_for_abortable_returns_result() -> None:
    controller = AbortController()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    future.set_result("ok")
    assert await wait_for_abortable(future, controller.signal, timeout=1.0) == "ok"


@pytest.mark.asyncio
async def test_cancel_all_survives_already_resolved_future() -> None:
    """ApprovalCenter.cancel_all 打断安全网：遇已解决审批不炸，pending 审批被取消。"""
    from src.coara.approval_center import ApprovalCenter, reset_approval_center_for_tests
    from src.coara.turn_context import reset_turn_context, set_turn_context

    class _ApprovalChannel:
        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def send_approval_request(self, frame: dict) -> bool:
            self.frames.append(dict(frame))
            return True

        async def send_approval_resolved(self, frame: dict) -> bool:  # noqa: ARG002
            return True

    options = [{"label": "同意", "description": ""}, {"label": "不同意", "description": ""}]
    reset_approval_center_for_tests()
    center = ApprovalCenter()
    channel = _ApprovalChannel()
    tokens = set_turn_context("!r:abort", interaction_channel=channel, source="matrix")  # type: ignore[arg-type]
    try:
        # 已在途且用户已回执的审批（future done）
        done_task = asyncio.create_task(center.request(question="q1", options=options, timeout_seconds=30))
        await asyncio.sleep(0.01)
        done_id = channel.frames[0]["approval_id"]
        assert center.resolve(done_id, approved=True, resolved_by="matrix")
        assert await done_task is True

        # 仍在等待的审批 → cancel_all 取消
        pending_task = asyncio.create_task(center.request(question="q2", options=options, timeout_seconds=30))
        await asyncio.sleep(0.01)

        assert center.cancel_all(reason="stop") >= 1
        with pytest.raises(OperationAborted):
            await pending_task
    finally:
        reset_turn_context(tokens)
        reset_approval_center_for_tests()


@pytest.mark.asyncio
async def test_abortable_lock_releases_on_abort_while_waiting() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    controller = AbortController()

    async def _abort_soon() -> None:
        await asyncio.sleep(0.01)
        controller.abort("stop")

    abort_task = asyncio.create_task(_abort_soon())
    with pytest.raises(OperationAborted):
        async with abortable_lock(lock, controller.signal):
            raise AssertionError("must not enter while lock held and aborted")
    await abort_task
    lock.release()
    # Lock must be free for the next waiter.
    await asyncio.wait_for(lock.acquire(), timeout=0.5)
    lock.release()


@pytest.mark.asyncio
async def test_matrix_confirm_send_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """矩阵 confirm（→ ApprovalCenter.request）卡片发不出 → RemotePromptDeliveryError。

    回合内经 turn room 定向投递；房间发送器返回 False 时 fail-closed 拒绝，
    不静默放行（审批只发回合来源端，无 standing 逃生门）。
    """
    from src.coara.approval_center import reset_approval_center_for_tests
    from src.coara.remote_channel import RemotePromptDeliveryError
    from src.coara.turn_context import reset_turn_context, set_turn_context
    from src.matrix_client import approval_bridge as bridge
    from src.matrix_client.remote_channel import MatrixRemoteInteractionChannel

    async def fail_send(_room: str, _content: dict) -> bool:
        return False

    reset_approval_center_for_tests()
    monkeypatch.setattr(bridge, "_ROOM_SENDER", fail_send)

    channel = MatrixRemoteInteractionChannel()
    tokens = set_turn_context("!r:test", send_text=lambda *a: True, interaction_channel=channel, source="matrix")
    try:
        with pytest.raises(RemotePromptDeliveryError, match="未能送达"):
            await channel.confirm("q", [{"label": "同意", "description": ""}])
    finally:
        reset_turn_context(tokens)
        reset_approval_center_for_tests()

"""ApprovalCenter — 内核唯一审批仲裁者（单一事实源）核心语义测试。

覆盖状态机关键路径：
- request 经回合端通道投递帧并等回执（approved/rejected、终态幂等）
- 超时 settle=timeout，终态帧经 send_terminal 回推对端
- 发送失败 / 无可达通道一律 fail-closed（绝不静默放行）
- cancel_all 打断安全网收口
- 持久化与重启加载（pending 记录重启即 cancelled）
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Any

import pytest

from src.coara.approval_center import (
    OUTCOME_APPROVED,
    OUTCOME_CANCELLED,
    OUTCOME_REJECTED,
    OUTCOME_TIMEOUT,
    ApprovalCenter,
)
from src.coara.remote_channel import RemotePromptDeliveryError
from src.core.abort import OperationAborted


def _options() -> list[dict[str, str]]:
    return [{"label": "同意", "description": "继续执行"}, {"label": "不同意", "description": ""}]


class _FakeChannel:
    """最小端通道：记录投递帧；send_approval_resolved 捕获终态回推。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.resolved: list[dict[str, Any]] = []
        self.send_ok = True

    async def send_approval_request(self, frame: dict[str, Any]) -> bool:
        self.requests.append(dict(frame))
        return self.send_ok

    async def send_approval_resolved(self, frame: dict[str, Any]) -> bool:
        self.resolved.append(dict(frame))
        return True

    async def send_text(self, body: str) -> bool:
        return True


@contextmanager
def _turn_with(channel: _FakeChannel):
    """注入回合端通道：ApprovalCenter.request 经 EndChannel 定向投递。"""
    from src.coara.turn_context import reset_turn_context, set_turn_context

    tokens = set_turn_context("web-1", interaction_channel=channel, source="web")  # type: ignore[arg-type]
    try:
        yield
    finally:
        reset_turn_context(tokens)


@pytest.mark.asyncio
async def test_resolve_rejects_wrong_actor() -> None:
    """Bound approval: only the turn originator may settle."""
    center = ApprovalCenter()
    channel = _FakeChannel()
    with _turn_with(channel):
        task = asyncio.create_task(center.request(question="允许执行吗？", options=_options(), timeout_seconds=30))
        await asyncio.sleep(0.01)
        approval_id = channel.requests[0]["approval_id"]
        assert center.get(approval_id).reply_actor == "web-1"  # type: ignore[union-attr]

        assert center.resolve(approval_id, approved=True, resolved_by="web", actor="other") is False
        assert center.get(approval_id).state == "pending"  # type: ignore[union-attr]

        assert center.resolve(approval_id, approved=True, resolved_by="web", actor="web-1") is True
        assert await task is True


@pytest.mark.asyncio
async def test_rebind_reply_actor_allows_new_connection() -> None:
    center = ApprovalCenter()
    channel = _FakeChannel()
    with _turn_with(channel):
        task = asyncio.create_task(center.request(question="允许执行吗？", options=_options(), timeout_seconds=30))
        await asyncio.sleep(0.01)
        approval_id = channel.requests[0]["approval_id"]
        assert center.rebind_reply_actor(approval_id, "web-2") is True
        assert center.resolve(approval_id, approved=True, resolved_by="web", actor="web-1") is False
        assert center.resolve(approval_id, approved=True, resolved_by="web", actor="web-2") is True
        assert await task is True
    center = ApprovalCenter()
    channel = _FakeChannel()
    with _turn_with(channel):
        task = asyncio.create_task(center.request(question="允许执行吗？", options=_options(), timeout_seconds=30))
        await asyncio.sleep(0.01)

        assert len(channel.requests) == 1
        frame = channel.requests[0]
        assert frame["question"] == "允许执行吗？"
        assert frame["options"][0]["label"] == "同意"
        assert "approval_id" in frame and "timeout_s" in frame and "workspace" in frame
        approval_id = frame["approval_id"]

        assert center.resolve(approval_id, approved=True, resolved_by="web", actor="web-1") is True
        assert await task is True
        assert center.get(approval_id) is not None
        assert center.get(approval_id).outcome == OUTCOME_APPROVED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_request_rejected_and_reply_idempotent() -> None:
    center = ApprovalCenter()
    channel = _FakeChannel()
    with _turn_with(channel):
        task = asyncio.create_task(center.request(question="允许执行吗？", options=_options(), timeout_seconds=30))
        await asyncio.sleep(0.01)
        approval_id = channel.requests[0]["approval_id"]

        assert center.resolve(approval_id, approved=False, resolved_by="web", actor="web-1") is True
        assert await task is False
        # 迟到/重复回执不生效：终态幂等，首回执裁定
        assert center.resolve(approval_id, approved=True, resolved_by="web", actor="web-1") is False
        assert center.get(approval_id).outcome == OUTCOME_REJECTED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_timeout_settles_and_pushes_resolved_frame() -> None:
    center = ApprovalCenter()
    channel = _FakeChannel()
    with _turn_with(channel):
        with pytest.raises(TimeoutError):
            await center.request(question="允许执行吗？", options=_options(), timeout_seconds=0.05)
        approval_id = channel.requests[0]["approval_id"]
        await asyncio.sleep(0)  # 终态帧由 settle 的 background task 回推
        assert center.get(approval_id).outcome == OUTCOME_TIMEOUT  # type: ignore[union-attr]
        assert any(r.get("approval_id") == approval_id and r.get("outcome") == "timeout" for r in channel.resolved)


@pytest.mark.asyncio
async def test_cancel_all_aborts_pending() -> None:
    center = ApprovalCenter()
    channel = _FakeChannel()
    with _turn_with(channel):
        task = asyncio.create_task(center.request(question="允许执行吗？", options=_options(), timeout_seconds=30))
        await asyncio.sleep(0.01)
        approval_id = channel.requests[0]["approval_id"]

        assert center.cancel_all(reason="interrupted") == 1
        with pytest.raises(OperationAborted):
            await task
        assert center.get(approval_id).outcome == OUTCOME_CANCELLED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_send_failure_fails_closed() -> None:
    """帧送不到来源端 → 拒绝并落终态，不存在静默放行路径。"""
    center = ApprovalCenter()
    channel = _FakeChannel()
    channel.send_ok = False
    with _turn_with(channel):
        with pytest.raises(RemotePromptDeliveryError):
            await center.request(question="允许执行吗？", options=_options(), timeout_seconds=30)
        approval_id = channel.requests[0]["approval_id"]
        assert center.get(approval_id).outcome == OUTCOME_CANCELLED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_no_reachable_channel_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """无回合端通道且 CLI modal 不可用 → 拒绝（fail-closed 兜底）。"""
    import sys

    center = ApprovalCenter()
    # 破坏 CLI modal import：走不到任何确认通道
    monkeypatch.setitem(sys.modules, "src.cli.interactive_prompt", None)
    with pytest.raises(RemotePromptDeliveryError):
        await center.request(question="允许执行吗？", options=_options(), timeout_seconds=30)


@pytest.mark.asyncio
async def test_terminal_record_persisted_and_reloaded(tmp_path: Any) -> None:
    store = tmp_path / "approvals.json"
    center = ApprovalCenter(store_path=store)
    channel = _FakeChannel()
    with _turn_with(channel):
        task = asyncio.create_task(center.request(question="允许执行吗？", options=_options(), timeout_seconds=30))
        await asyncio.sleep(0.01)
        approval_id = channel.requests[0]["approval_id"]
        center.resolve(approval_id, approved=True, resolved_by="web", actor="web-1")
        await task

    assert store.exists()
    reloaded = ApprovalCenter(store_path=store)
    record = reloaded.get(approval_id)
    assert record is not None and record.outcome == OUTCOME_APPROVED
    assert reloaded.pending_records() == []


def test_pending_record_cancelled_on_restart_load(tmp_path: Any) -> None:
    """进程重启后 pending 记录无 future 可等：加载即 cancelled，端侧重连补偿收口。"""
    store = tmp_path / "approvals.json"
    payload = {
        "version": 1,
        "approvals": [
            {
                "approval_id": "ap-old",
                "question": "允许执行吗？",
                "options": [{"label": "同意", "description": ""}],
                "timeout_s": 300,
                "source": "web",
                "workspace": "v8",
                "created_at": 1_700_000_000.0,
                "expires_at": 1_700_000_300.0,
                "state": "pending",
                "outcome": "",
                "resolved_by": "",
                "resolved_at": 0.0,
            }
        ],
    }
    store.write_text(json.dumps(payload), encoding="utf-8")

    center = ApprovalCenter(store_path=store)

    record = center.get("ap-old")
    assert record is not None
    assert record.state == "terminal"
    assert record.outcome == OUTCOME_CANCELLED
    assert record.resolved_by == "restart"

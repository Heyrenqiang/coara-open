"""B 类会话配置命令确认门（execute_command interaction_channel 语义）。

覆盖三态：他端占用需确认（批准放行 / 拒绝取消 / 送达失败 fail-closed）、
本端占用或空闲直接放行、纯查看形态（/model 无参等）不过门。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from src.coara.approval_center import get_approval_center
from src.coara.commands.registry import CommandArgs, maybe_confirm_session_config


class _FakeChannel:
    """模拟端交互通道：记录请求帧，可控制送达成败。"""

    def __init__(self, deliver_ok: bool = True) -> None:
        self.deliver_ok = deliver_ok
        self.requested: list[dict[str, Any]] = []

    async def send_approval_request(self, frame: dict[str, Any]) -> bool:
        self.requested.append(frame)
        return self.deliver_ok

    async def send_approval_resolved(self, frame: dict[str, Any]) -> bool:
        return True


def _busy_coara(source: str = "web") -> SimpleNamespace:
    return SimpleNamespace(
        has_active_turn=lambda: True,
        _active_turn_source=source,
        workspace_dir="D:/ws/main",
    )


def _args(name: str, raw: str, target: Any, origin: str = "cli-attached") -> CommandArgs:
    parts = raw.split()
    sub = parts[1].lower() if len(parts) >= 2 else None
    value = parts[2] if len(parts) >= 3 else None
    args = CommandArgs(name=name, parts=parts, raw=raw, sub=sub, value=value)
    args.target_coara = target
    args.origin_source = origin
    return args


async def _wait_requested(channel: _FakeChannel, timeout: float = 3.0) -> None:
    for _ in range(100):
        if channel.requested:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("确认请求未送达通道")


class TestConfirmGateSemantics:
    async def test_other_end_busy_approve_passes(self) -> None:
        """他端（web）占用 + 用户批准 → 放行（返回 None）。"""
        channel = _FakeChannel(deliver_ok=True)
        coara = _busy_coara("web")
        args = _args("new", "/new", coara)
        task = asyncio.create_task(maybe_confirm_session_config(args, SimpleNamespace(), channel))
        await _wait_requested(channel)
        approval_id = channel.requested[0]["approval_id"]
        get_approval_center().resolve(approval_id, approved=True, resolved_by="test")
        result = await asyncio.wait_for(task, timeout=3.0)
        assert result is None

    async def test_other_end_busy_reject_cancels(self) -> None:
        """他端占用 + 用户拒绝 → 返回取消结果（不执行）。"""
        channel = _FakeChannel(deliver_ok=True)
        coara = _busy_coara("web")
        args = _args("new", "/new", coara)
        task = asyncio.create_task(maybe_confirm_session_config(args, SimpleNamespace(), channel))
        await _wait_requested(channel)
        approval_id = channel.requested[0]["approval_id"]
        get_approval_center().resolve(approval_id, approved=False, resolved_by="test")
        result = await asyncio.wait_for(task, timeout=3.0)
        assert result is not None
        assert result.data.get("cancelled") is True

    async def test_other_end_busy_delivery_failure_fail_closed(self) -> None:
        """他端占用 + 送达失败（无通道回执能力）→ fail-closed 取消。"""
        channel = _FakeChannel(deliver_ok=False)
        coara = _busy_coara("web")
        args = _args("new", "/new", coara)
        result = await maybe_confirm_session_config(args, SimpleNamespace(), channel)
        assert result is not None
        assert result.data.get("cancelled") is True

    async def test_same_end_own_turn_passes_without_confirm(self) -> None:
        """本端自己的回合占用 → 不弹确认直接放行。"""
        channel = _FakeChannel()
        coara = _busy_coara("cli-attached")
        args = _args("new", "/new", coara, origin="cli-attached")
        result = await maybe_confirm_session_config(args, SimpleNamespace(), channel)
        assert result is None
        assert not channel.requested

    async def test_idle_session_passes_without_confirm(self) -> None:
        """会话空闲 → 不弹确认直接放行。"""
        channel = _FakeChannel()
        coara = SimpleNamespace(has_active_turn=lambda: False, _active_turn_source="")
        args = _args("new", "/new", coara)
        result = await maybe_confirm_session_config(args, SimpleNamespace(), channel)
        assert result is None
        assert not channel.requested

    async def test_readonly_model_list_passes_without_confirm(self) -> None:
        """/model 无参（查看列表）→ 非写动作，不过门。"""
        channel = _FakeChannel()
        coara = _busy_coara("web")
        args = _args("model", "/model", coara)
        result = await maybe_confirm_session_config(args, SimpleNamespace(), channel)
        assert result is None
        assert not channel.requested

    async def test_system_background_turn_requires_confirm(self) -> None:
        """系统后台回合占用（background 唤醒）→ 触发确认：掐后台交付有代价。"""
        channel = _FakeChannel(deliver_ok=True)
        coara = _busy_coara("background")
        args = _args("new", "/new", coara)
        task = asyncio.create_task(maybe_confirm_session_config(args, SimpleNamespace(), channel))
        await _wait_requested(channel)
        assert channel.requested
        approval_id = channel.requested[0]["approval_id"]
        get_approval_center().resolve(approval_id, approved=False, resolved_by="test")
        result = await asyncio.wait_for(task, timeout=3.0)
        assert result is not None
        assert result.data.get("cancelled") is True

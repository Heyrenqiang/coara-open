"""回归：``_awakened_turn_active`` 是回合级状态，早退也必须清掉。

置位在调用方（唤醒驱动 root/attach_ws/web_server 在 process_message 前置 True），
清除原只在回合 finally——三条早退（账户门禁拦截 / 延迟 /new 存活期 / 排队回合
已被取消）都在拿到 ``_process_lock`` 之前或回合 try 之外返回，绕过清除。残留会
让该会话此后每轮 turn_end 都带 awakened=True，活动时钟再也不刷新（空闲自动新
会话与依赖空闲的 janitor 被系统性推迟）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers import make_test_coara


@pytest.mark.asyncio
async def test_awakened_flag_cleared_when_deferred_new_session_blocks(tmp_path: Path) -> None:
    """延迟 /new 存活期的早退（拿到锁之前 return）。"""
    coara = make_test_coara(tmp_path)
    coara._awakened_turn_active = True
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    coara._deferred_new_session_task = pending  # 未完成 → 拒绝新回合
    try:
        chunks = [chunk async for chunk in coara.process_message("你好")]
        assert any("正在开始新会话" in chunk for chunk in chunks)
    finally:
        pending.cancel()

    assert coara._awakened_turn_active is False


@pytest.mark.asyncio
async def test_awakened_flag_cleared_when_turn_gate_blocks(tmp_path: Path, monkeypatch) -> None:
    """账户门禁拦截的早退（最早的一条 return）。"""
    coara = make_test_coara(tmp_path)
    coara._awakened_turn_active = True

    async def _blocking_gate(_home: object) -> SimpleNamespace:
        return SimpleNamespace(ok=False, reason="测试拦截")

    monkeypatch.setattr("src.ext.turn_gate", _blocking_gate)
    monkeypatch.setattr("src.core.coara_home.resolve_coara_home", lambda _ws: tmp_path)

    chunks = [chunk async for chunk in coara.process_message("你好")]
    assert any("测试拦截" in chunk for chunk in chunks)
    assert coara._awakened_turn_active is False

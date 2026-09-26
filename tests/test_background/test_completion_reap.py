"""后台任务收官加固：孙进程握管时的强制收官（收集型管道拦截已移除）。"""

from __future__ import annotations

import asyncio

import pytest

from src.background.bash_runner import BashBackgroundRunner


@pytest.mark.asyncio
async def test_reap_orphaned_pipes_closes_transport_after_grace():
    """进程已退出（returncode 已置）但 wait() 仍挂：宽限后强制关闭传输端。"""

    class _FakeTransport:
        closed = False

        def close(self):
            self.closed = True

    class _FakeProcess:
        returncode = 0

        def __init__(self):
            self._transport = _FakeTransport()

    proc = _FakeProcess()
    await BashBackgroundRunner._reap_orphaned_pipes(proc, grace=0.05)
    assert proc._transport.closed


@pytest.mark.asyncio
async def test_reap_orphaned_pipes_cancellable_while_running():
    """进程未退出时收割协程安静等待，被取消不抛错。"""

    class _FakeProcess:
        returncode = None

        _transport = None

    proc = _FakeProcess()
    task = asyncio.create_task(BashBackgroundRunner._reap_orphaned_pipes(proc, grace=0.05))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    assert not proc._transport

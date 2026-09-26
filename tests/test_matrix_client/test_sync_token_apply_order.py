"""P0-16: durable Matrix sync token advances only after batch apply."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.matrix_client.sync_helpers import (
    MatrixMessageDispatcher,
    _current_sync_gen,
    run_matrix_sync_loop,
)
from src.matrix_client.sync_token import load_gomatrix_sync_token


@pytest.mark.asyncio
async def test_token_not_persisted_while_handler_still_running(tmp_path: Path) -> None:
    """Crash window: token must stay behind until the scheduled handler finishes."""
    token_path = tmp_path / "matrix_sync_token"
    dispatcher = MatrixMessageDispatcher()
    release = asyncio.Event()
    started = asyncio.Event()
    client = SimpleNamespace(next_batch=None)
    syncs = 0

    async def _sync(**_kwargs):
        nonlocal syncs
        syncs += 1
        if syncs == 1:
            client.next_batch = "s1"
            gen = _current_sync_gen.get()
            assert gen is not None

            async def slow() -> None:
                started.set()
                await release.wait()

            dispatcher.schedule(slow(), label="slow-msg")
            return SimpleNamespace(next_batch="s1")
        raise asyncio.CancelledError()

    client.sync = _sync
    loop_task = asyncio.create_task(
        run_matrix_sync_loop(
            client,
            token_path=token_path,
            dispatcher=dispatcher,
            label="test",
            timeout_ms=10,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    assert load_gomatrix_sync_token(token_path) is None

    release.set()
    await dispatcher.drain(timeout=2.0)
    assert load_gomatrix_sync_token(token_path) == "s1"
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


@pytest.mark.asyncio
async def test_busy_drop_freezes_durable_token(tmp_path: Path) -> None:
    """Busy-drop is not a successful apply — durable cursor must not advance."""
    token_path = tmp_path / "matrix_sync_token"
    dispatcher = MatrixMessageDispatcher()
    hog_started = asyncio.Event()
    release_hog = asyncio.Event()
    client = SimpleNamespace(next_batch=None)
    syncs = 0

    async def _sync(**_kwargs):
        nonlocal syncs
        syncs += 1
        if syncs == 1:
            client.next_batch = "s1"

            async def hog() -> None:
                hog_started.set()
                await release_hog.wait()

            dispatcher.schedule(hog(), label="hog", session_key="ws")
            return SimpleNamespace(next_batch="s1")
        if syncs == 2:
            client.next_batch = "s2"

            async def dropped() -> None:
                return None

            async def on_busy() -> None:
                return None

            dispatcher.schedule(
                dropped(),
                label="doomed",
                session_key="ws",
                on_busy=on_busy,
                busy_timeout=0.05,
            )
            return SimpleNamespace(next_batch="s2")
        raise asyncio.CancelledError()

    client.sync = _sync
    loop_task = asyncio.create_task(
        run_matrix_sync_loop(
            client,
            token_path=token_path,
            dispatcher=dispatcher,
            label="test",
            timeout_ms=10,
        )
    )
    await asyncio.wait_for(hog_started.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    assert load_gomatrix_sync_token(token_path) is None
    # gen2 busy-drops while gen1 still holds the session lock
    await asyncio.sleep(0.35)
    assert load_gomatrix_sync_token(token_path) is None
    release_hog.set()
    await dispatcher.drain(timeout=2.0)
    assert load_gomatrix_sync_token(token_path) == "s1"
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


@pytest.mark.asyncio
async def test_later_batch_waits_for_earlier_apply(tmp_path: Path) -> None:
    """Out-of-order handler completion still persists tokens in sync order."""
    token_path = tmp_path / "matrix_sync_token"
    dispatcher = MatrixMessageDispatcher()
    release_a = asyncio.Event()
    b_done = asyncio.Event()
    client = SimpleNamespace(next_batch=None)
    syncs = 0

    async def _sync(**_kwargs):
        nonlocal syncs
        syncs += 1
        if syncs == 1:
            client.next_batch = "s1"

            async def slow_a() -> None:
                await release_a.wait()

            dispatcher.schedule(slow_a(), label="a", session_key="a")
            return SimpleNamespace(next_batch="s1")
        if syncs == 2:
            client.next_batch = "s2"

            async def fast_b() -> None:
                b_done.set()

            dispatcher.schedule(fast_b(), label="b", session_key="b")
            return SimpleNamespace(next_batch="s2")
        raise asyncio.CancelledError()

    client.sync = _sync
    loop_task = asyncio.create_task(
        run_matrix_sync_loop(
            client,
            token_path=token_path,
            dispatcher=dispatcher,
            label="test",
            timeout_ms=10,
        )
    )
    await asyncio.wait_for(b_done.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    # s2 applied first, but durable must not jump past unfinished s1
    assert load_gomatrix_sync_token(token_path) is None
    release_a.set()
    await dispatcher.drain(timeout=2.0)
    assert load_gomatrix_sync_token(token_path) == "s2"
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


@pytest.mark.asyncio
async def test_idle_sync_persists_token_without_handlers(tmp_path: Path) -> None:
    token_path = tmp_path / "matrix_sync_token"
    dispatcher = MatrixMessageDispatcher()
    client = SimpleNamespace(next_batch=None)
    syncs = 0

    async def _sync(**_kwargs):
        nonlocal syncs
        syncs += 1
        if syncs == 1:
            client.next_batch = "s7"
            return SimpleNamespace(next_batch="s7")
        raise asyncio.CancelledError()

    client.sync = _sync
    loop_task = asyncio.create_task(
        run_matrix_sync_loop(
            client,
            token_path=token_path,
            dispatcher=dispatcher,
            label="test",
            timeout_ms=10,
        )
    )
    for _ in range(40):
        if load_gomatrix_sync_token(token_path) == "s7":
            break
        await asyncio.sleep(0.05)
    assert load_gomatrix_sync_token(token_path) == "s7"
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task
